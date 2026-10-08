# 日志初始化（P1-B1）
# 职责：提供统一的日志格式与级别配置，避免各处 print 造成日志不可追溯。
# 说明：P7-B1 会升级为结构化日志（JSON）+ Sentry 上报，此处保持最小可用实现。

import logging
import sys

# 统一的日志格式：时间 | 级别 | 模块名 | 消息
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """配置全局根日志器。

    Args:
        level: 日志级别字符串（DEBUG/INFO/WARNING/ERROR）。
    """
    # 强制重建 handler，避免 uvicorn 启动或单测重复调用时重复输出
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 第三方库日志降噪：httpx 每次请求都打 INFO 会淹没业务日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """按模块名获取日志器（各模块使用 __name__ 传入即可）。"""
    return logging.getLogger(name)
