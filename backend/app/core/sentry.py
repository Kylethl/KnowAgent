# 错误上报（P7-B1：Sentry）
# ---------------------------------------------------------------------
# 职责：把「未捕获异常 / 5xx / 关键业务失败」上报到 Sentry（或兼容的 GlitchTip /
#       Sentry 私有化部署），并保证**未配置 DSN 时零外发流量**。
#
# 合规前提（《需求清单》P7 功能2「数据不出域」）：
#   - DSN 由部署方显式配置才启用；默认空 = 完全关闭；
#   - SENTRY_SEND_DEFAULT_PII 默认 False：不上报 IP、Cookie、请求头等个人信息；
#   - 私有化场景可把 DSN 指向自建 Sentry，数据同样不出域。
#
# 依赖策略：sentry_sdk 为可选依赖（见 requirements.txt）。
#   未安装或未配置 DSN 时本模块全部函数退化为 no-op，
#   绝不让「错误上报」成为服务启动/运行的硬依赖。

from typing import Any, Dict, Optional

from .logging import get_logger

logger = get_logger(__name__)

# 全局标记：是否已成功初始化（避免重复 init 与无效调用）
_initialized = False
# 是否发生了「配置了 DSN 但 SDK 未安装」的情形（状态页据此提示运维）
_sdk_missing = False


def init_sentry(settings) -> bool:
    """按配置初始化 Sentry（幂等，可重复调用）。

    Args:
        settings: app.config.Settings 实例。

    Returns:
        True 表示已启用并完成初始化；False 表示未启用（无 DSN / 未安装 SDK）。
    """
    global _initialized, _sdk_missing

    # 已启用过就不再重复初始化（重复 init 会让 SDK 忽略新配置且产生告警）
    if _initialized:
        return True

    dsn = (getattr(settings, "SENTRY_DSN", "") or "").strip()
    if not dsn:
        # 默认路径：不引入任何外发流量，仅记一条 debug 便于确认「确实没开」
        logger.debug("Sentry 未启用（SENTRY_DSN 为空）")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        # 配置了 DSN 却没装 SDK：属于部署缺陷，用 warning 明确告知（不阻断启动）
        _sdk_missing = True
        logger.warning(
            "已配置 SENTRY_DSN 但未安装 sentry-sdk，错误上报将不可用。"
            "请执行：pip install 'sentry-sdk[fastapi]>=2.0'"
        )
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=settings.sentry_environment,
            # 版本号与 FastAPI 应用保持一致，便于在 Sentry 中按发布版本对比回归
            release="knowagent@0.1.0",
            # 追踪采样率：默认 0（只收错误），大流量下 tracing 成本高
            traces_sample_rate=float(getattr(settings, "SENTRY_TRACES_SAMPLE_RATE", 0.0)),
            # PII：合规默认关闭，绝不上报 IP/Cookie/请求头
            send_default_pii=bool(getattr(settings, "SENTRY_SEND_DEFAULT_PII", False)),
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            # 上报前过滤：健康检查/指标抓取失败没有价值，剔除以免刷屏
            before_send=_before_send,
        )
        _initialized = True
        logger.info(
            "Sentry 已启用：environment=%s，traces_sample_rate=%s，send_pii=%s",
            settings.sentry_environment,
            getattr(settings, "SENTRY_TRACES_SAMPLE_RATE", 0.0),
            getattr(settings, "SENTRY_SEND_DEFAULT_PII", False),
        )
        return True
    except Exception as exc:  # noqa: BLE001 —— 上报组件故障不得影响主服务
        logger.warning("Sentry 初始化失败（已忽略，不影响服务运行）：%s", exc)
        return False


def _before_send(event: Dict[str, Any], hint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """上报前的最后一道过滤。

    Args:
        event: Sentry 事件字典（含 request 信息）。
        hint: 附加信息（异常对象等）。

    Returns:
        处理后的事件；返回 None 表示丢弃该事件。
    """
    request = event.get("request") or {}
    url = str(request.get("url") or "")
    # 探针与指标抓取路径：失败无排障价值，且高频会消耗配额
    if any(url.endswith(path) for path in ("/health", "/health/ready", "/metrics")):
        return None
    # 双保险：即便 SDK 版本忽略了 send_default_pii，也在这里抹掉敏感头
    headers = request.get("headers") or {}
    for key in ("authorization", "cookie", "x-api-key"):
        headers.pop(key, None)
    return event


def capture_exception(exc: BaseException, **scope: Any) -> None:
    """上报一个异常（未初始化时为 no-op）。

    Args:
        exc: 异常对象。
        **scope: 附加到该事件的标签（如 request_id / user_id / action）。
    """
    if not _initialized:
        return
    try:
        import sentry_sdk

        # 用 push_scope 承载附加标签，避免影响后续事件的全局作用域
        with sentry_sdk.push_scope() as scoped:
            for key, value in scope.items():
                if value:
                    scoped.set_tag(key, str(value)[:200])
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 —— 上报失败绝不能再抛异常
        pass


def capture_message(message: str, level: str = "error", **scope: Any) -> None:
    """上报一条消息级事件（用于「不抛异常但必须告警」的场景，如备份失败）。"""
    if not _initialized:
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scoped:
            for key, value in scope.items():
                if value:
                    scoped.set_tag(key, str(value)[:200])
            sentry_sdk.capture_message(message, level=level)
    except Exception:  # noqa: BLE001
        pass


def is_enabled() -> bool:
    """是否已成功启用（状态页/合规自检用）。"""
    return _initialized


def is_sdk_missing() -> bool:
    """是否出现「配置了 DSN 但 SDK 缺失」的部署缺陷。"""
    return _sdk_missing
