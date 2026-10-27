# Celery 应用定义（P3-B2：异步入库任务）
# ---------------------------------------------------------------------
# 用途：为「文档入库」这类耗时任务（解析 + 向量化 + 写库）提供异步执行能力，
#       避免阻塞 HTTP 请求（《需求清单》P3 功能2「Celery 异步任务触发」）。
#
# 启动 worker（在 backend/ 目录执行）：
#   celery -A app.tasks.celery_app:celery_app worker -l info
#
# 说明：
#   - broker / backend 均使用 Redis（P3-Dep1 引入，见 docker/docker-compose.dev.yml）；
#   - 本模块只**定义**应用对象，不建立真实连接，因此导入是安全且廉价的；
#   - Redis 不可达时，入库会自动回退为进程内后台任务（见 services/indexing_service
#     .enqueue_index_document），保证本地无 Redis 也能跑通入库闭环。

from celery import Celery

from ..config import get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)

settings = get_settings()

# 应用名使用项目名，便于在监控/日志中识别
celery_app = Celery(
    "knowagent",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    # ---------- 序列化 ----------
    # 统一用 JSON：可读性高、跨语言安全（避免 pickle 的反序列化风险）
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # ---------- 时区 ----------
    # 内部一律按 UTC 存储，展示时再转本地时区
    timezone="Asia/Shanghai",
    enable_utc=True,
    # ---------- 可靠性 ----------
    # 任务执行完再 ack：worker 中途崩溃时任务会被重新投递，避免入库任务丢失
    task_acks_late=True,
    # 入库任务较重（单篇可能数十秒），一次只预取 1 个，避免单个 worker 囤积任务
    worker_prefetch_multiplier=1,
    # 单个任务的硬超时（秒）：超时强杀，防止异常任务长期占用 worker
    task_time_limit=600,
    task_soft_time_limit=540,
    # 结果过期时间（秒）：入库结果仅供排查，无需长期保留
    result_expires=3600,
)

logger.debug(
    "Celery 应用已定义：broker=%s，backend=%s",
    settings.CELERY_BROKER_URL,
    settings.CELERY_RESULT_BACKEND,
)
