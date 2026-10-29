# 日志初始化（P1-B1 建立；P7-B1 升级为结构化日志）
# ---------------------------------------------------------------------
# 职责：
#   1. 统一日志格式与级别，避免各处 print 造成日志不可追溯；
#   2. P7 起支持两种格式：
#        - text：人类可读单行文本（本地开发的默认值）
#        - json：单行 JSON（生产采集 ELK / Loki / 云日志服务用）
#   3. 自动为每条日志注入请求上下文（request_id / user_id / ip），
#      使「一条请求的全部日志」可被 request_id 串联，故障定位不必再靠时间猜。
#   4. 可选落盘 + 按大小轮转（非容器部署时便于留档备查）。
#
# 设计要求（合规相关）：
#   - **绝不记录请求体、令牌、密码、API Key**：日志只记录元信息（方法/路径/状态/耗时）。
#     SSE 的 /chat 用 ?token= 传令牌，因此访问日志默认不写 query string
#     （见 config.ACCESS_LOG_INCLUDE_QUERY，默认 False）。
#   - 日志目录/文件由配置决定，不写死路径。
#
# 使用方式（业务代码）：
#   from ..core.logging import get_logger
#   logger = get_logger(__name__)
#   logger.info("事件=%s 用户=%s", "document.review", user.username)
#
# 说明：本模块不做「日志脱敏」的兜底扫描——那会带来不可预期的性能损耗，
# 且在错误的位置掩盖问题。真正的约束是**不把敏感值传给日志**（见上面第 1 条）。

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import request_context

# 文本格式：时间 | 级别 | 模块 | [request_id] 消息
_TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(request_id_suffix)s%(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# LogRecord 的内置属性名：JSON 序列化时用于挑出「业务自定义字段」
# （业务通过 logger.info(..., extra={"foo": "bar"}) 附加的字段会出现在 record.__dict__ 中）
_STANDARD_RECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
    "request_id", "user_id", "ip",
}


class RequestContextFilter(logging.Filter):
    """把 contextvars 中的请求上下文注入每条日志记录。

    为什么用 Filter 而不是在每条日志里手写：
        业务代码不应该为了「日志带上 request_id」而修改每个调用点；
        用 Filter 在记录进入 Handler 前统一补齐，调用点保持干净。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """补齐上下文属性并生成文本格式所需的 request_id 前缀。

        Args:
            record: 待输出的日志记录。

        Returns:
            恒为 True（本过滤器只补充字段，不过滤任何日志）。
        """
        request_id = request_context.get_request_id()
        record.request_id = request_id or "-"
        # 文本格式的前缀：有 request_id 时显示 [abc12345]，否则什么都不加
        record.request_id_suffix = f"[{request_id[:8]}] " if request_id else ""
        record.user_id = request_context.get_user_id() or ""
        record.ip = request_context.get_client_ip() or ""
        return True


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志格式化器（生产采集用）。

    输出字段固定为：
        ts / level / logger / msg / request_id / user_id / ip / exc
    业务附加字段（extra）会被平铺进同一层 JSON，便于在日志系统里直接按字段检索。
    """

    def format(self, record: logging.LogRecord) -> str:
        """把日志记录序列化为一行 JSON。

        Args:
            record: 已由 RequestContextFilter 补齐上下文的日志记录。

        Returns:
            JSON 字符串（不换行；异常栈以 "exc" 字段携带）。
        """
        payload: Dict[str, Any] = {
            # 时间统一用 UTC ISO8601：跨时区部署时排序与对齐不会错乱
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        # 用户与 IP 只在有值时输出，避免空字符串污染索引
        for key in ("user_id", "ip"):
            value = getattr(record, key, "")
            if value:
                payload[key] = value

        # 业务自定义字段（logger.info(..., extra={...})）：平铺进 JSON
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_KEYS or key.startswith("_"):
                continue
            # 只接受可 JSON 序列化的简单类型，其余转字符串，保证一定能输出
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
            else:
                payload[key] = str(value)

        # 异常栈：以字符串携带，日志平台可直接展示
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text

        # ensure_ascii=False：中文保持可读，避免日志里全是 \uXXXX
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logging(
    level: str = "INFO",
    fmt: str = "text",
    file_path: str = "",
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 10,
) -> None:
    """配置全局根日志器（可在启动时或单测中重复调用）。

    Args:
        level: 日志级别字符串（DEBUG/INFO/WARNING/ERROR）。
        fmt: 日志格式，text 或 json（大小写不敏感）。
        file_path: 日志文件绝对路径；为空表示只输出到 stdout。
        max_bytes: 单文件大小上限（字节），仅 file_path 非空时生效。
        backup_count: 轮转保留的历史文件数。

    说明：
        - 每次都先移除已存在的 handler，避免 uvicorn 启动或单测重复调用时重复输出；
        - 文件写入失败（目录不可创建/无权限）时**降级为仅 stdout** 并打印告警，
          不让「日志落盘」这种可选项阻断服务启动。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # 统一格式器：json 或 text
    context_filter = RequestContextFilter()
    if (fmt or "").strip().lower() == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(_TEXT_FORMAT, datefmt=_DATE_FORMAT)

    # ---------- stdout handler（容器部署的主通道） ----------
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(context_filter)
    root.addHandler(stream_handler)

    # ---------- 可选的滚动文件 handler ----------
    if file_path:
        try:
            # 目录可能不存在：自动创建（exist_ok 保证并发启动不报错）
            os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                file_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(context_filter)
            root.addHandler(file_handler)
        except OSError as exc:  # noqa: BLE001 —— 落盘失败不应阻断启动
            # 此时 root 已配好 stdout handler，用 root 直接告警（避免再建 logger）
            root.warning("日志文件不可用（已降级为仅 stdout）：path=%s，原因=%s", file_path, exc)

    root.setLevel((level or "INFO").upper())

    # 第三方库日志降噪：httpx 每次请求都打 INFO 会淹没业务日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # uvicorn 的访问日志与我们的可观测中间件重复（且不带 request_id），统一关掉
    logging.getLogger("uvicorn.access").disabled = True
    # sqlalchemy 的 INFO 会把每条 SQL 打出来，噪音过大（需要时用 DB_ECHO 打开）
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """按模块名获取日志器（各模块使用 __name__ 传入即可）。"""
    return logging.getLogger(name)


def log_extra(**kwargs: Any) -> Dict[str, Any]:
    """构造 `logger.info(..., extra=...)` 的参数，过滤掉与内置属性冲突的键。

    为什么需要它：
        logging 对 extra 中的键有严格限制——一旦与 LogRecord 内置属性同名
        （如 "message"、"filename"），会在运行时抛 KeyError 把日志变成异常。
        统一走本函数可以避免这类「日志把业务搞挂」的问题。

    Args:
        **kwargs: 业务希望附加到日志的字段。

    Returns:
        可安全传入 extra 的字典。
    """
    return {k: v for k, v in kwargs.items() if k not in _STANDARD_RECORD_KEYS}


def current_level() -> Optional[int]:
    """返回根日志器当前级别（便于 /ops/status 展示，未配置时为 None）。"""
    return logging.getLogger().level
