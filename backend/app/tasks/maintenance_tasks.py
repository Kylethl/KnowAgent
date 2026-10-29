# 运维定时任务（P7-DB1 / P7-Dep1）
# ---------------------------------------------------------------------
# 职责：把「数据库备份」「备份保留清理」「审计日志过期清理」注册为 Celery 定时任务，
#       由 Celery beat 按 crontab 触发（见 celery_app.py 的 beat_schedule）。
#
# 为什么用 Celery beat 而不是宿主机 cron：
#   1. 与业务任务共用一套 worker/Redis 与日志，运维不需要在两处维护；
#   2. 多副本部署时只有一个 beat 实例（部署约定见 deploy/k8s），不会重复触发；
#   3. 任务结果进 Redis，可通过 Flower/日志统一查看。
#
# 为什么同时提供 shell 脚本（scripts/backup_postgres.sh）：
#   私有化交付现场常常不允许跑 Celery（例如只部署了单容器），
#   此时用宿主机 crontab 调脚本更简单。两条路径产出同样的备份文件与 manifest，
#   状态页读的是目录，因此**无论用哪种方式备份，页面都能看到**。
#
# 失败语义：任务内部捕获异常并返回结构化结果（不抛出），避免 Celery 无意义重试；
#          失败会写错误日志 + 指标 + 审计，同时（若配置了）上报 Sentry。

import asyncio

from ..core import metrics, sentry
from ..core.logging import get_logger
from ..db.engine import SessionLocal
from ..services import audit_service, backup_service
from .celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(name="maintenance.backup_database")
def backup_database_task() -> dict:
    """定时执行一次 PostgreSQL 备份（beat 每日触发）。

    Returns:
        形如 {"ok": bool, "file": str|None, "size_bytes": int, "error": str|None}。
    """

    async def _run() -> dict:
        """在独立事件循环里执行备份 + Redis 持久化检查。"""
        # 1) 数据库备份（失败不抛，转成结构化结果）
        try:
            item = await backup_service.run_backup(trigger="schedule", operator="celery-beat")
            result = {
                "ok": True,
                "file": item["name"],
                "size_bytes": item["size_bytes"],
                "error": None,
            }
            # 审计：定时任务同样要留痕（合规检查会问「备份是否按计划执行」）
            await audit_service.record(
                "ops.backup.trigger",
                username="celery-beat",
                role="system",
                resource_type="backup",
                resource_id=item["name"],
                detail={"trigger": "schedule", "size_bytes": item["size_bytes"]},
            )
        except Exception as exc:  # noqa: BLE001 —— 定时任务失败必须留痕且不重试
            logger.error("定时备份失败：%s", exc)
            result = {"ok": False, "file": None, "size_bytes": 0, "error": str(exc)[:500]}
            await audit_service.record(
                "ops.backup.trigger",
                result="failure",
                username="celery-beat",
                role="system",
                resource_type="backup",
                detail={"trigger": "schedule", "reason": str(exc)[:300]},
            )
            # 备份失败是「明天就可能丢数据」级别的问题，必须主动告警
            sentry.capture_message(f"定时备份失败：{exc}", level="error", task="backup")

        # 2) Redis 持久化检查：备份窗口顺带确认队列数据也已落盘
        try:
            redis_status = await backup_service.redis_persistence_status(trigger_save=True)
            result["redis"] = {
                "reachable": redis_status["reachable"],
                "aof_enabled": redis_status["aof_enabled"],
                "persistence_ok": redis_status["persistence_ok"],
            }
            if redis_status["reachable"] and not redis_status["persistence_ok"]:
                logger.warning(
                    "Redis 持久化未生效：aof_enabled=%s，rdb_status=%s",
                    redis_status["aof_enabled"],
                    redis_status["rdb_last_status"],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 持久化检查失败（不影响备份结果）：%s", exc)

        return result

    payload = asyncio.run(_run())
    if payload.get("ok"):
        logger.info(
            "定时备份完成：file=%s，size=%.1fMB",
            payload["file"],
            (payload.get("size_bytes") or 0) / (1024 * 1024),
        )
    return payload


@celery_app.task(name="maintenance.prune_backups")
def prune_backups_task() -> dict:
    """按保留策略清理过期备份（每日在备份之后执行）。

    Returns:
        {"removed": 删除数量}
    """
    try:
        removed = backup_service.prune_backups()
        logger.info("备份保留清理完成：删除 %d 个文件", removed)
        return {"removed": removed}
    except Exception as exc:  # noqa: BLE001
        logger.error("备份保留清理失败：%s", exc)
        return {"removed": 0, "error": str(exc)[:300]}


@celery_app.task(name="maintenance.prune_audit_logs")
def prune_audit_logs_task() -> dict:
    """清理超过保留期的审计日志（AUDIT_RETENTION_DAYS）。

    Returns:
        {"deleted": 删除条数}
    """

    async def _run() -> int:
        """在独立事件循环里执行清理（复用服务层的实现，保证口径一致）。"""
        from ..config import get_settings

        async with SessionLocal() as session:
            return await audit_service.prune(session, get_settings().AUDIT_RETENTION_DAYS)

    try:
        deleted = asyncio.run(_run())
        # 指标：清理量异常增大往往意味着有脚本在刷审计，值得注意
        metrics.inc_audit("ops.audit.prune")
        return {"deleted": deleted}
    except Exception as exc:  # noqa: BLE001
        logger.error("审计日志清理失败：%s", exc)
        return {"deleted": 0, "error": str(exc)[:300]}
