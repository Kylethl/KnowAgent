# P7 可观测性测试（结构化日志 / 指标 / 请求上下文 / 中间件）
# ---------------------------------------------------------------------
# 覆盖《需求清单》P7 功能1「后端结构化日志」与 P7-Dep1「Prometheus 监控」的实现细节。
#
# 原则：不依赖 Prometheus 服务器、不依赖日志采集系统、不依赖真实 Redis/DB，
#      只验证「我们产出的格式与字段是否正确」——这才是接收集成时最容易出错的地方。

import json
import logging
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import metrics, request_context
from app.core.logging import (
    JsonFormatter,
    RequestContextFilter,
    get_logger,
    log_extra,
    setup_logging,
)
from app.core.middleware import ObservabilityMiddleware

# 测试用工作目录：放在仓库内（沙箱环境禁止写 /tmp，用固定目录更稳）
_WORK_ROOT = Path(__file__).resolve().parent / ".pytest_p7_work"


@pytest.fixture()
def work_dir():
    """提供一个隔离的临时目录（用完即删）。"""
    target = _WORK_ROOT / uuid4().hex
    target.mkdir(parents=True, exist_ok=True)
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)



# ---------------------------------------------------------------------
# 请求上下文
# ---------------------------------------------------------------------


def test_request_context_bind_and_snapshot():
    """写入的上下文应能被快照完整读出，清理后归零。"""
    request_context.bind_request_context("req-1234567890", "10.0.0.9", "pytest-agent")
    request_context.bind_user("11111111-1111-1111-1111-111111111111", "alice")

    snap = request_context.snapshot()
    assert snap["request_id"] == "req-1234567890"
    assert snap["ip"] == "10.0.0.9"
    assert snap["username"] == "alice"

    request_context.clear_request_context()
    assert request_context.get_request_id() == ""
    assert request_context.get_user_id() == ""


def test_extract_client_ip_prefers_forwarded_for():
    """X-Forwarded-For 第一段最接近真实客户端，应优先采用。"""
    ip = request_context.extract_client_ip("203.0.113.7, 10.0.0.1", "10.0.0.2", "127.0.0.1")
    assert ip == "203.0.113.7"


def test_extract_client_ip_falls_back_to_real_ip_and_peer():
    """缺失 XFF 时用 X-Real-IP，再缺失时用 TCP 对端地址。"""
    assert request_context.extract_client_ip(None, "198.51.100.5", "127.0.0.1") == "198.51.100.5"
    assert request_context.extract_client_ip(None, None, "192.0.2.1") == "192.0.2.1"


def test_extract_client_ip_truncates_long_value():
    """超长值应被截断（列宽 64），避免脏头把审计字段撑爆。"""
    ip = request_context.extract_client_ip("x" * 200, None, None)
    assert len(ip) == 64


# ---------------------------------------------------------------------
# JSON 日志格式
# ---------------------------------------------------------------------


def _make_record(msg: str = "hello %s", args: tuple = ("world",)) -> logging.LogRecord:
    """构造一条日志记录（不走 logger，便于精确断言格式）。"""
    return logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_json_formatter_outputs_expected_fields():
    """JSON 日志必须含 ts/level/logger/msg/request_id，且能正确格式化参数。"""
    request_context.bind_request_context("abcdef1234567890", "10.1.1.1", "UA")
    record = _make_record()
    RequestContextFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["msg"] == "hello world"
    assert payload["request_id"] == "abcdef1234567890"
    assert payload["ip"] == "10.1.1.1"
    # ts 必须是 ISO8601（UTC），便于跨时区排序
    assert payload["ts"].endswith("+00:00")
    request_context.clear_request_context()


def test_json_formatter_keeps_business_extra_fields():
    """通过 extra 传入的业务字段应平铺到 JSON 顶层，便于日志平台按字段检索。"""
    # args 必须显式传空元组：默认的 ("world",) 会让 "%s" 缺失时报格式化错误
    record = _make_record("审计事件", ())
    record.audit_action = "document.review"
    RequestContextFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))
    assert payload["audit_action"] == "document.review"


def test_json_formatter_includes_exception_stack():
    """异常栈应写入 exc 字段（否则线上只能看到一句错误信息）。"""
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record("失败", ())
        record.exc_info = sys.exc_info()
        RequestContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
    assert "ValueError" in payload["exc"]


def test_log_extra_filters_reserved_keys():
    """与 LogRecord 内置属性同名的键必须被过滤，否则 logging 会抛 KeyError。"""
    extra = log_extra(message="x", filename="y", module="z", custom=1)
    assert extra == {"custom": 1}


def test_setup_logging_writes_json_file(work_dir):
    """配置了文件路径时应落盘，且内容是可解析的单行 JSON。"""
    log_file = work_dir / "app.log"
    setup_logging(level="INFO", fmt="json", file_path=str(log_file), backup_count=1)
    try:
        get_logger("test.p7").info("落盘测试 %s", "ok")
        # 手动 flush：RotatingFileHandler 有缓冲，不 flush 读不到内容
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8").strip()
        assert text, "日志文件不应为空"
        first = json.loads(text.splitlines()[0])
        assert first["msg"] == "落盘测试 ok"
    finally:
        # 恢复默认配置，避免影响后续测试（它们可能断言日志格式）
        setup_logging(level="WARNING", fmt="text")


# ---------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------


def test_normalize_path_compresses_ids():
    """/documents/<uuid>/chunks/<uuid> 应归一化为 :id，避免指标基数爆炸。"""
    uuid = "3f2a1b4c-5d6e-7f80-9a1b-2c3d4e5f6071"
    assert metrics.normalize_path(f"/documents/{uuid}/chunks/{uuid}") == (
        "/documents/:id/chunks/:id"
    )
    assert metrics.normalize_path("/admin/documents/123/review") == "/admin/documents/:id/review"
    # 普通路径保持原样
    assert metrics.normalize_path("/chat/quota") == "/chat/quota"


def test_metrics_render_exposes_known_series():
    """渲染结果应包含我们定义的核心指标名与归一化后的路径标签。"""
    metrics.observe_http("GET", "/documents/123", 200, 0.02)
    metrics.inc_llm_request("success")
    metrics.set_dependency("postgres", True)

    payload, content_type = metrics.render()
    text = payload.decode("utf-8")
    assert "knowagent_http_requests_total" in text
    assert 'path="/documents/:id"' in text
    assert "knowagent_llm_requests_total" in text
    assert "knowagent_dependency_up" in text
    # content_type 必须是 Prometheus 能识别的文本类型
    assert "text/plain" in content_type


def test_metrics_backend_is_reported():
    """后端名称只允许两种取值（装了 prometheus_client / 内置降级）。"""
    assert metrics.backend_name() in ("prometheus_client", "builtin")


def test_metrics_inc_helpers_are_safe_with_zero_tokens():
    """0 token 不应产生无意义的序列（避免 label 组合膨胀）。"""
    metrics.inc_llm_tokens(0, 0)  # 不抛异常即为通过


# ---------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------


def _build_app() -> FastAPI:
    """构造带可观测性中间件的最小应用（不引入真实业务依赖）。"""
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/ping")
    async def ping():
        """测试路由：返回 request_id，便于断言中间件是否生效。"""
        return {"request_id": request_context.get_request_id()}

    @app.get("/boom")
    async def boom():
        """测试路由：抛异常，验证中间件兜底日志不影响异常传播。"""
        raise RuntimeError("kaboom")

    return app


def test_middleware_generates_and_returns_request_id():
    """未携带 X-Request-ID 时应生成一个，并回写到响应头。"""
    client = TestClient(_build_app())
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.headers["x-request-id"]
    # 路由内读到的 request_id 必须与响应头一致（证明上下文确实生效）
    assert resp.json()["request_id"] == resp.headers["x-request-id"]


def test_middleware_reuses_valid_incoming_request_id():
    """上游网关传入的合法 request_id 应被透传，保持全链路同一个 ID。"""
    client = TestClient(_build_app())
    resp = client.get("/ping", headers={"X-Request-ID": "gateway-abc-123"})
    assert resp.headers["x-request-id"] == "gateway-abc-123"
    assert resp.json()["request_id"] == "gateway-abc-123"


def test_middleware_rejects_unsafe_incoming_request_id():
    """含非法字符的 request_id 不能透传（防头注入与日志污染）。"""
    client = TestClient(_build_app())
    resp = client.get("/ping", headers={"X-Request-ID": "bad id with space"})
    assert resp.headers["x-request-id"] != "bad id with space"


def test_middleware_does_not_swallow_exceptions():
    """中间件只做观测，异常必须继续向上抛（由框架转 500）。"""
    client = TestClient(_build_app(), raise_server_exceptions=True)
    with pytest.raises(RuntimeError):
        client.get("/boom")
