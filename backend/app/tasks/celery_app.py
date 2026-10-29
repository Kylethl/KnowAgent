# Celery 应用定义（P3-B2：异步入库任务）
# ---------------------------------------------------------------------
# 用途：为「文档入库」这类耗时任务（解析 + 向量化 + 写库）提供异步执行能力，
#       避免阻塞 HTTP 请求（《需求清单》P3 功能2「Celery 异步任务触发」）。
#
# 启动 worker（在 backend/ 目录执行）：
#   celery -A app.tasks.celery_app:celery_app worker -l info
#
# P5 起支持多队列（媒体解析吃算力，需独立/扩容）：
#   文档入库 worker（默认队列）：celery -A app.tasks.celery_app:celery_app worker -l info -Q celery
#   媒体入库 worker（OCR/ASR）：celery -A app.tasks.celery_app:celery_app worker -l info -Q media -c 1
#   说明：媒体队列建议 -c 1（并发 1），因为一次转写就占满 CPU/带宽，
#        并发太高反而互相拖慢；需要扩容时横向多加几个媒体 worker 即可。
#
# 说明：
#   - broker / backend 均使用 Redis（P3-Dep1 引入，见 docker/docker-compose.dev.yml）；
#   - 本模块只**定义**应用对象，不建立真实连接，因此导入是安全且廉价的；
#   - Redis 不可达时，入库会自动回退为进程内后台任务（见 services/indexing_service
#     .enqueue_index_document），保证本地无 Redis 也能跑通入库闭环。

from celery import Celery
from celery.schedules import crontab

from ..config import get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)

settings = get_settings()

# 应用名使用项目名，便于在监控/日志中识别
celery_app = Celery(
    "knowagent",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    # P7 修复：显式声明任务模块。
    # 为什么必须写在这里：worker 启动时只导入本模块（-A app.tasks.celery_app:celery_app），
    # 若任务模块没被 include，worker 收到消息会报 "Received unregistered task"——
    # 表现为「任务投递成功但永远不执行」，是排查成本极高的一类故障。
    include=[
        "app.tasks.indexing_tasks",
        "app.tasks.maintenance_tasks",
    ],
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
    # 单个任务的硬超时（秒）：超时强杀，防止异常任务长期占用 worker。
    # P5 起音视频转写可能持续数分钟（长音频要分段逐段调用模型），
    # 故把上限从 600s 放宽到 1800s；软超时提前 60s 便于任务自行收尾。
    task_time_limit=1800,
    task_soft_time_limit=1740,
    # ---------- 队列 ----------
    # 显式声明 media 队列：即使暂时没有独立 worker 也不会因为队列未声明而投递失败
    task_default_queue="celery",
    task_queues=None,  # 使用默认队列配置（Redis broker 支持动态队列）
    # 结果过期时间（秒）：入库结果仅供排查，无需长期保留
    result_expires=3600,
)

logger.debug(
    "Celery 应用已定义：broker=%s，backend=%s",
    settings.CELERY_BROKER_URL,
    settings.CELERY_RESULT_BACKEND,
)


# ---------------------------------------------------------------------
# 定时任务编排（P7-DB1 / P7-Dep1：定时备份与保留清理）
# ---------------------------------------------------------------------
# 启动 beat（与 worker 分开跑，生产建议单副本）：
#   celery -A app.tasks.celery_app:celery_app beat -l info
#
# 时间安排（错开执行，避免备份 IO 与清理任务互相拖慢）：
#   03:00  数据库备份（BACKUP_SCHEDULE_HOUR 可配）
#   03:40  备份保留清理（等备份写完再删旧的，避免边删边占 IO）
#   04:10  审计日志过期清理（低频、量小）
_beat_schedule = {}
if settings.BACKUP_ENABLED:
    _beat_schedule["p7-daily-database-backup"] = {
        "task": "maintenance.backup_database",
        # crontab 使用 celery 配置的时区（见上方 timezone=Asia/Shanghai）
        "schedule": crontab(hour=settings.BACKUP_SCHEDULE_HOUR, minute=0),
    }
    _beat_schedule["p7-prune-old-backups"] = {
        "task": "maintenance.prune_backups",
        "schedule": crontab(hour=settings.BACKUP_SCHEDULE_HOUR, minute=40),
    }
if settings.AUDIT_ENABLED:
    _beat_schedule["p7-prune-audit-logs"] = {
        "task": "maintenance.prune_audit_logs",
        "schedule": crontab(hour=4, minute=10),
    }

celery_app.conf.beat_schedule = _beat_schedule
logger.debug("已注册定时任务：%s", ", ".join(_beat_schedule.keys()) or "(无)")

