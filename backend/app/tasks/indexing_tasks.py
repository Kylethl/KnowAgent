# 入库异步任务（P3-B2）
# ---------------------------------------------------------------------
# 职责：把 services/indexing_service.index_document 包装为 Celery 任务，
#       由 worker 异步执行「解析 → 切片 → 向量化 → 写向量」。

import asyncio

from ..core.logging import get_logger
from ..services import indexing_service
from .celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(name="indexing.index_document", bind=True)
def index_document_task(self, document_id: str) -> dict:
    """异步执行文档入库。

    说明：Celery worker 是同步进程，而入库管线基于 async/await，
          故这里用 asyncio.run 启动一个独立的事件循环执行。

    失败处理：
      - 业务失败（解析/向量化/存储）已在服务层写入 documents.last_index_error
        并把状态回退为 'approved'，因此这里**不再抛出**异常触发 Celery 自动重试
        （重试对「文件本身有问题」的场景无意义，只会浪费 embedding 费用）；
      - 任务返回结构化结果，便于 worker 日志与结果后端排查。

    Args:
        self: Celery 任务实例（bind=True 提供，可用于重试与状态更新）。
        document_id: 文档 ID 字符串。

    Returns:
        形如 {"document_id": ..., "ok": bool, "chunk_count": int, "error": str|None} 的结果字典。
    """
    try:
        chunk_count = asyncio.run(indexing_service.index_document(document_id))
        logger.info("入库任务完成：document_id=%s，切片数=%d", document_id, chunk_count)
        return {"document_id": document_id, "ok": True, "chunk_count": chunk_count, "error": None}
    except Exception as exc:  # noqa: BLE001 —— 记录后吞掉，避免无意义的自动重试
        logger.error("入库任务失败：document_id=%s，原因=%s", document_id, exc)
        return {"document_id": document_id, "ok": False, "chunk_count": 0, "error": str(exc)[:500]}
