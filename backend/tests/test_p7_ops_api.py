# P7 运维接口测试（/ops/*、/metrics、/chat/quota、429 与就绪探针）
# ---------------------------------------------------------------------
# 覆盖《需求清单》P7 的系统状态页数据契约与限流对外语义。
#
# 隔离策略：
#   - 依赖覆盖伪造管理员与数据库，不连真实 PG；
#   - 服务层函数用 monkeypatch 替换，验证「路由 → 契约」这一层，
#     真实探测逻辑由各自的服务测试覆盖（如 test_p7_backup.py）。

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.ops as ops_module
import app.main as main_module
from app.core.errors import BackupError, RateLimitExceeded
from app.core.security import get_current_user_id, require_admin
from app.db.engine import get_db
from app.db.models import AuditLog, User
from app.main import create_app


def _make_admin() -> User:
    """构造内存管理员对象。"""
    user = User()
    user.id = uuid4()
    user.username = "ops-admin"
    user.role = "admin"
    user.is_active = True
    return user


@pytest.fixture()
def client():
    """带管理员身份与假数据库的测试客户端。"""
    app = create_app()
    admin = _make_admin()
    app.dependency_overrides[require_admin] = lambda: admin
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return TestClient(app)


# ---------------------------------------------------------------------
# 鉴权边界
# ---------------------------------------------------------------------


def test_ops_endpoints_require_auth():
    """/ops/* 是运维面，未登录必须 401（不能靠前端守卫）。"""
    client = TestClient(create_app())
    for path in ("/ops/status", "/ops/audit-logs", "/ops/backups", "/ops/compliance"):
        assert client.get(path).status_code == 401, path


def test_ops_endpoints_forbid_non_admin(monkeypatch):
    """普通用户访问 /ops/* 应 403（RBAC 由依赖统一裁决，不靠前端守卫）。"""
    from app.core.security import get_current_user

    app = create_app()

    async def _normal_user():
        """伪造一个普通用户，让 require_admin 判定越权。"""
        user = User()
        user.id = uuid4()
        user.username = "normal"
        user.role = "user"
        return user

    app.dependency_overrides[get_current_user] = _normal_user
    client = TestClient(app)
    resp = client.get("/ops/status")
    assert resp.status_code == 403


# ---------------------------------------------------------------------
# 状态总览
# ---------------------------------------------------------------------


def test_status_returns_aggregated_payload(client, monkeypatch):
    """状态接口应原样回传服务层聚合结果（契约字段完整）。"""
    payload = {
        "app": {
            "name": "KnowAgent API",
            "version": "0.1.0",
            "env": "development",
            "uptime_seconds": 120,
            "log_format": "json",
            "log_level": "INFO",
            "sentry_enabled": False,
            "sentry_sdk_missing": False,
            "metrics_enabled": True,
            "metrics_backend": "builtin",
        },
        "services": [
            {
                "name": "postgres",
                "label": "PostgreSQL",
                "status": "ok",
                "detail": "PostgreSQL 16",
                "latency_ms": 1.2,
            }
        ],
        "llm": {
            "configured": True,
            "model": "deepseek-flash",
            "rate_limit_enabled": True,
            "rate_limit_per_minute": 20,
            "rate_limit_per_day": 300,
            "daily_token_quota": 200000,
            "quota": None,
        },
        "backup": {
            "enabled": True,
            "dir": "/data/backups",
            "retention_days": 14,
            "retention_count": 30,
            "schedule_hour": 3,
            "next_run_hint": None,
            "last": None,
            "count": 0,
            "total_bytes": 0,
            "last_verified": False,
        },
        "audit": {
            "total": 0,
            "today": 0,
            "failures": 0,
            "last_event_at": None,
            "window_days": 7,
            "top_actions": [],
        },
        "residency": {"mode": "standard", "region": "cn", "strict": False, "allowlist": [], "targets": []},
        "metrics": {"enabled": True, "path": "/metrics", "token_required": False, "grafana_url": ""},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    async def _fake_collect(db, user_id=None):
        """替身：返回固定聚合结果（不触发真实依赖探测）。"""
        return payload

    monkeypatch.setattr(ops_module.ops_service, "collect_status", _fake_collect)

    resp = client.get("/ops/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app"]["version"] == "0.1.0"
    assert body["services"][0]["status"] == "ok"
    assert body["residency"]["mode"] == "standard"


# ---------------------------------------------------------------------
# 审计查询
# ---------------------------------------------------------------------


def _audit_row() -> AuditLog:
    """构造一条内存审计行。"""
    row = AuditLog()
    row.id = uuid4()
    row.user_id = uuid4()
    row.username = "alice"
    row.role = "admin"
    row.action = "document.review"
    row.resource_type = "document"
    row.resource_id = "doc-1"
    row.result = "success"
    row.ip = "10.0.0.1"
    row.request_id = "req-1"
    row.detail = {"action": "approved"}
    row.created_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    return row


def test_audit_logs_returns_items_and_total(client, monkeypatch):
    """查询应返回 items/total/limit/offset，且动作带中文标签。"""

    async def _fake_list(db, **kwargs):
        """替身：返回一条记录与总数。"""
        return [_audit_row()], 1

    async def _fake_record(*args, **kwargs):
        """替身：审计写入本身不参与本用例断言。"""
        return None

    monkeypatch.setattr(ops_module.audit_service, "list_audit_logs", _fake_list)
    monkeypatch.setattr(ops_module.audit_service, "record", _fake_record)

    resp = client.get("/ops/audit-logs?result=success&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["action_label"] == "审核文档"
    assert body["items"][0]["ip"] == "10.0.0.1"


def test_audit_logs_rejects_invalid_user_id(client):
    """非法 UUID 应返回 400（而不是让 SQL 抛错变成 500）。"""
    resp = client.get("/ops/audit-logs?user_id=not-a-uuid")
    assert resp.status_code == 400


def test_audit_logs_rejects_invalid_result_filter(client):
    """result 只接受 success/failure（参数校验由 FastAPI 完成 → 422）。"""
    resp = client.get("/ops/audit-logs?result=maybe")
    assert resp.status_code == 422


def test_audit_actions_returns_dictionary(client):
    """动作字典应覆盖核心事件，供前端下拉使用。"""
    resp = client.get("/ops/audit-logs/actions")
    assert resp.status_code == 200
    values = {item["value"] for item in resp.json()}
    assert "document.review" in values and "auth.login_failed" in values


# ---------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------


def test_list_backups_returns_status_and_items(client, monkeypatch):
    """备份列表接口一次返回「策略总览 + 文件列表」，前端只需一次请求。"""
    monkeypatch.setattr(
        ops_module.backup_service,
        "backup_status",
        lambda: {
            "enabled": True,
            "dir": "/data/backups",
            "retention_days": 14,
            "retention_count": 30,
            "schedule_hour": 3,
            "next_run_hint": None,
            "last": None,
            "count": 1,
            "total_bytes": 1024,
            "last_verified": True,
        },
    )
    monkeypatch.setattr(
        ops_module.backup_service,
        "list_backups",
        lambda limit=50: [
            {
                "name": "knowagent-20261028-030000.sql.gz",
                "size_bytes": 1024,
                "created_at": "2026-10-28T03:00:10",
                "sha256": "a" * 64,
                "elapsed_ms": 1200,
                "trigger": "schedule",
                "operator": "celery-beat",
                "verified": True,
                "restore_hint": "bash scripts/restore_postgres.sh x",
                "retention_days": 14,
            }
        ],
    )

    resp = client.get("/ops/backups")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["count"] == 1
    assert body["items"][0]["trigger"] == "schedule"


def test_trigger_backup_returns_item_and_writes_audit(client, monkeypatch):
    """手动备份成功应返回备份信息，并写入一条审计。"""
    recorded: list = []

    async def _fake_run(trigger, operator):
        """替身：直接返回一份「已校验」的备份描述。"""
        return {
            "name": "knowagent-20261028-120000.sql.gz",
            "size_bytes": 2048,
            "created_at": "2026-10-28T12:00:00",
            "sha256": "b" * 64,
            "elapsed_ms": 900,
            "trigger": trigger,
            "operator": operator,
            "verified": True,
            "restore_hint": "bash scripts/restore_postgres.sh knowagent-20261028-120000.sql.gz",
            "retention_days": 14,
        }

    async def _fake_record(action, **kwargs):
        """替身：捕获审计动作。"""
        recorded.append((action, kwargs.get("result", "success")))

    monkeypatch.setattr(ops_module.backup_service, "run_backup", _fake_run)
    monkeypatch.setattr(ops_module.audit_service, "record", _fake_record)

    resp = client.post("/ops/backups")
    assert resp.status_code == 200
    assert resp.json()["operator"] == "ops-admin"
    assert recorded == [("ops.backup.trigger", "success")]


def test_trigger_backup_failure_returns_500_and_audits(client, monkeypatch):
    """备份失败应 500 且 detail 可读，同时写一条 failure 审计（便于追责）。"""
    recorded: list = []

    async def _boom(trigger, operator):
        """替身：模拟 pg_dump 失败。"""
        raise BackupError("pg_dump 退出码 1：permission denied")

    async def _fake_record(action, **kwargs):
        """替身：捕获审计。"""
        recorded.append((action, kwargs.get("result", "success")))

    monkeypatch.setattr(ops_module.backup_service, "run_backup", _boom)
    monkeypatch.setattr(ops_module.audit_service, "record", _fake_record)

    resp = client.post("/ops/backups")
    assert resp.status_code == 500
    assert "permission denied" in resp.json()["detail"]
    assert recorded == [("ops.backup.trigger", "failure")]


def test_prune_backups_returns_removed_count(client, monkeypatch):
    """清理接口应回传删除数量与最新总览。"""
    monkeypatch.setattr(ops_module.backup_service, "prune_backups", lambda: 3)
    monkeypatch.setattr(
        ops_module.backup_service,
        "backup_status",
        lambda: {
            "enabled": True,
            "dir": "/data/backups",
            "retention_days": 14,
            "retention_count": 0,
            "schedule_hour": 3,
            "next_run_hint": None,
            "last": None,
            "count": 0,
            "total_bytes": 0,
            "last_verified": False,
        },
    )

    async def _noop(*args, **kwargs):
        """替身：忽略审计。"""
        return None

    monkeypatch.setattr(ops_module.audit_service, "record", _noop)

    resp = client.post("/ops/backups/prune")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 3


# ---------------------------------------------------------------------
# 合规自检
# ---------------------------------------------------------------------


def test_compliance_returns_report(client, monkeypatch):
    """合规报告应含汇总与检查项列表。"""

    async def _fake_report():
        """替身：返回一份最小报告。"""
        return {
            "generated_at": "2026-10-28T12:00:00+08:00",
            "mode": "standard",
            "region": "cn",
            "summary": {"pass": 2, "warn": 1, "fail": 0},
            "items": [
                {
                    "key": "audit_enabled",
                    "title": "审计日志",
                    "category": "审计",
                    "status": "pass",
                    "detail": "已开启",
                    "hint": "",
                }
            ],
        }

    monkeypatch.setattr(ops_module.ops_service, "build_compliance_report", _fake_report)

    resp = client.get("/ops/compliance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"pass": 2, "warn": 1, "fail": 0}
    assert body["items"][0]["key"] == "audit_enabled"


# ---------------------------------------------------------------------
# 配额
# ---------------------------------------------------------------------


def test_chat_quota_returns_remaining(client, monkeypatch):
    """配额查询应返回用量与上限（前端据此在提问前提示）。"""
    from app.core.rate_limit import QuotaStatus

    async def _fake_status(user_id):
        """替身：返回一份「接近上限」的快照。"""
        return QuotaStatus(
            enabled=True,
            allowed=True,
            minute_used=18,
            minute_limit=20,
            day_used=30,
            day_limit=300,
            tokens_used=500,
            tokens_quota=200000,
            source="memory",
        )

    app = client.app
    app.dependency_overrides[get_current_user_id] = lambda: uuid4()
    monkeypatch.setattr(ops_module.rate_limit, "get_quota_status", _fake_status)
    monkeypatch.setattr("app.api.chat.rate_limit.get_quota_status", _fake_status)

    resp = client.get("/chat/quota")
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is True
    assert body["minute_used"] == 18
    assert body["minute_limit"] == 20


def test_chat_returns_429_with_retry_after(monkeypatch):
    """配额超限时 /chat 必须返回 429 + Retry-After，且不进流式生成。"""
    from app.core.security import get_current_user_id_sse
    import app.api.chat as chat_module

    app = create_app()
    user_id = uuid4()
    app.dependency_overrides[get_current_user_id_sse] = lambda: user_id

    def _deny(user_id=None):
        """替身：直接抛限流异常，验证全局处理器。"""
        raise RateLimitExceeded(
            "提问过于频繁（上限 20 次/分钟）", scope="minute", limit=20, retry_after=35
        )

    # 覆盖守门依赖本身：等价于「check_and_consume 判定为拒绝」
    app.dependency_overrides[chat_module._guard_llm_quota] = _deny

    client = TestClient(app)
    resp = client.get("/chat?message=你好")
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "35"
    body = resp.json()
    assert body["scope"] == "minute"
    assert body["limit"] == 20


# ---------------------------------------------------------------------
# 指标与就绪探针
# ---------------------------------------------------------------------


def test_metrics_endpoint_returns_prometheus_text(monkeypatch):
    """/metrics 应输出 Prometheus 文本格式（不要求管理员身份）。"""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "METRICS_TOKEN", "")
    client = TestClient(create_app())
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "knowagent_http_requests_total" in resp.text


def test_metrics_endpoint_requires_token_when_configured(monkeypatch):
    """配置了抓取令牌后，未带令牌应返回 404（隐藏端点存在性）。"""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "METRICS_TOKEN", "s3cret")
    client = TestClient(create_app())

    assert client.get("/metrics").status_code == 404
    assert client.get("/metrics?token=wrong").status_code == 404
    ok = client.get("/metrics?token=s3cret")
    assert ok.status_code == 200
    assert "knowagent_" in ok.text


def test_ready_probe_reflects_database_state(monkeypatch):
    """/health/ready 应根据数据库可用性返回 200/503（K8s 就绪探针语义）。"""
    async def _ok():
        """替身：数据库可用。"""
        return True

    monkeypatch.setattr(main_module, "db_ping", _ok)
    client = TestClient(create_app())
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["database"] == "ok"

    async def _down():
        """替身：数据库不可用。"""
        return False

    monkeypatch.setattr(main_module, "db_ping", _down)
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


def test_health_liveness_stays_lightweight():
    """/health 只报进程状态，不应因外部依赖不可用而失败。"""
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
