# 可观测性中间件（P7-B1 / P7-Dep1）
# ---------------------------------------------------------------------
# 作用：为每个 HTTP 请求统一完成四件事，业务代码零侵入：
#   1. 生成/透传 X-Request-ID，并写入 contextvars（日志与审计自动带上）；
#   2. 记录访问日志（方法/路径/状态/耗时/IP），超阈值标记 slow；
#   3. 上报 Prometheus 指标（次数 + 耗时直方图 + 并发数）；
#   4. 把请求 ID 回写到响应头，前端报错时可直接把它贴给运维定位。
#
# 为什么用**纯 ASGI 中间件**而不是 FastAPI 的 BaseHTTPMiddleware：
#   BaseHTTPMiddleware 会把响应包一层 anyio 任务，对 StreamingResponse（SSE）
#   存在已知的缓冲/取消传播问题——本项目的核心功能就是 SSE 流式对话，
#   用纯 ASGI 包裹 send 只为观测，不改变消息时序，对 SSE 完全透明。
#
# 合规约定（重要）：
#   - 访问日志**不记录请求体**，也不默认记录 query string
#     （/chat 用 ?token= 传令牌，写进日志等于把令牌落盘）；
#   - 不记录响应体（可能是知识库正文）；
#   - 只记录元信息，足以定位性能与错误，不足以还原用户数据。

import time
import uuid
from typing import Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import get_settings
from . import metrics, request_context, sentry
from .logging import get_logger

# 访问日志使用独立 logger 名：便于在日志系统里单独设保留策略与采样
_access_logger = get_logger("knowagent.access")


def _new_request_id(incoming: Optional[str]) -> str:
    """生成或复用请求 ID。

    优先复用上游网关传入的 X-Request-ID（保持全链路同 ID），
    但只接受「长度合理、字符安全」的值，防止把任意字符串写进日志/响应头
    （头注入与日志污染风险）。

    Args:
        incoming: 请求头中的 X-Request-ID（可能为 None）。

    Returns:
        16~64 字符的请求 ID。
    """
    if incoming:
        candidate = incoming.strip()
        # 只允许字母数字与 - _ ，长度 8~64
        if 8 <= len(candidate) <= 64 and all(
            ch.isalnum() or ch in "-_" for ch in candidate
        ):
            return candidate
    # 默认：uuid4 去连字符的前 16 位，足够唯一且更短（日志更紧凑）
    return uuid.uuid4().hex[:16]


class ObservabilityMiddleware:
    """可观测性中间件：请求 ID + 访问日志 + Prometheus 指标。"""

    def __init__(self, app: ASGIApp) -> None:
        """保存被包裹的 ASGI 应用。

        Args:
            app: 下游 ASGI 应用（FastAPI 实例）。
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """处理一次请求。

        非 HTTP 协议（lifespan / websocket）直接透传，不做任何观测包装。

        Args:
            scope: ASGI 作用域。
            receive: 接收通道。
            send: 发送通道。
        """
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        headers = {
            # ASGI 头是 bytes 且 key 为小写
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers") or []
        }
        method = scope.get("method", "GET")
        raw_path = scope.get("path", "/")
        client = scope.get("client") or (None, None)
        client_ip = request_context.extract_client_ip(
            headers.get("x-forwarded-for"),
            headers.get("x-real-ip"),
            client[0] if client else None,
        )
        request_id = _new_request_id(headers.get("x-request-id"))

        # 写入上下文：后续日志/审计/限流都能直接读到，无需层层传参
        request_context.bind_request_context(
            request_id=request_id,
            client_ip=client_ip,
            user_agent=headers.get("user-agent", ""),
        )

        started = time.perf_counter()
        status_code = 500  # 若下游在发响应前抛异常，按 500 记
        response_started = False

        async def send_wrapper(message: Message) -> None:
            """包装 send：抓响应状态码并回写 X-Request-ID。"""
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
                response_started = True
                # 回写请求 ID：前端把错误详情连同它一起反馈，运维可直接 grep
                raw_headers = list(message.get("headers") or [])
                raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": raw_headers}
            await send(message)

        if settings.METRICS_ENABLED:
            metrics.inc_in_progress(1)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # 兜底：未捕获异常在这里留一条带 request_id 的错误日志，
            # 并上报 Sentry（未配置时是 no-op），随后**继续抛出**由上层处理。
            duration_ms = (time.perf_counter() - started) * 1000
            _access_logger.exception(
                "请求异常：%s %s（耗时 %.0fms）",
                method,
                raw_path,
                duration_ms,
                extra={
                    "http_method": method,
                    "http_path": raw_path,
                    "http_status": status_code,
                    "duration_ms": round(duration_ms, 2),
                    "slow": True,
                },
            )
            sentry.capture_exception(exc, request_id=request_id, path=raw_path)
            raise
        finally:
            duration = time.perf_counter() - started
            duration_ms = duration * 1000

            if settings.METRICS_ENABLED:
                metrics.inc_in_progress(-1)
                # 只有真正产生响应的请求才计入流量指标，避免异常请求污染分布
                if response_started:
                    metrics.observe_http(method, raw_path, status_code, duration)

            # 访问日志：可按需关闭（例如高频探活场景）
            if settings.ACCESS_LOG_ENABLED:
                path_for_log = raw_path
                query = scope.get("query_string", b"").decode("latin-1")
                # 默认不记录 query：/chat 的 ?token= 是敏感信息
                if query and settings.ACCESS_LOG_INCLUDE_QUERY:
                    path_for_log = f"{raw_path}?{query}"

                slow = duration_ms >= settings.METRICS_SLOW_REQUEST_MS
                # 5xx 用 warning，慢请求用 warning，其余 info——便于按级别筛关键请求
                if status_code >= 500:
                    level_log = _access_logger.error
                elif slow or status_code >= 400:
                    level_log = _access_logger.warning
                else:
                    level_log = _access_logger.info
                level_log(
                    "%s %s → %d（%.0fms%s）",
                    method,
                    path_for_log,
                    status_code,
                    duration_ms,
                    "，慢请求" if slow else "",
                    extra={
                        "http_method": method,
                        "http_path": path_for_log,
                        "http_status": status_code,
                        "duration_ms": round(duration_ms, 2),
                        "slow": slow,
                    },
                )

            # 清理上下文，避免同一任务后续逻辑（如后台任务）串用请求身份
            request_context.clear_request_context()
