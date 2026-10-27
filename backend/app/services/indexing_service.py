# 文档入库服务（P3-B2：RAG 管线编排）
# ---------------------------------------------------------------------
# 职责：把「审核通过的文档」加工为可检索的向量切片，并维护文档入库状态。
#
# 对应《需求清单》P3 功能2：
#   - 后端：Celery 异步任务触发——解析 Word（python-docx）/Markdown → 切片 →
#           调 embedding → 写向量；支持重入库 / 下线
#   - 数据存储：向量写 document_chunks；documents.status = indexed
#
# 管线（index_document）：
#   ① 校验（文档存在 + 已审核通过 + 未正在入库）
#   ② 置 status='indexing'
#   ③ 读原始文件 → parse_document → chunk_blocks
#   ④ embed_texts 批量向量化（分批 + 重试）
#   ⑤ 删除该文档旧切片 → 写入新切片（保证重入库幂等）
#   ⑥ 置 status='indexed' + 回填 chunk_count / indexed_at
#   任一步失败：status 回退为 'approved'（可重试），并把原因写入 last_index_error。
#
# 设计要点：
#   1. **重入库幂等**：先删旧切片再插新的，重复入库不会产生重复数据；
#      (doc_id, chunk_index) 唯一约束再兜一层。
#   2. **失败可观测**：失败原因落 last_index_error，管理员可在后台看到「入库失败」。
#   3. **投递与执行解耦**：enqueue_index_document 优先投递 Celery；
#      Redis 不可达时返回 False，由调用方回退为进程内后台任务，保证开发期闭环可跑。

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..core.errors import (
    DocumentNotFound,
    IndexingInProgress,
    IndexingNotAllowed,
)
from ..core.logging import get_logger
from ..db.engine import SessionLocal
from ..db.models import Document, DocumentChunk
from ..services import embedding as embedding_mod
from ..services.chunker import Chunk, chunk_blocks
from ..services.parser import DocumentParseError, parse_document
from ..services.storage import StorageError, build_storage

logger = get_logger(__name__)

# 「入库中」僵死判定阈值（分钟）：
# 若文档停留在 indexing 超过该时长仍未完成，说明上次入库因进程重启/异常中断，
# 此时允许重新入库，避免文档被永久卡死在 indexing 状态。
INDEXING_STALE_MINUTES = 10

# 单篇文档的最大切片数（防御性上限）：
# 防止超大文档产生海量切片拖垮 embedding 费用与写入耗时。
MAX_CHUNKS_PER_DOCUMENT = 500


def _parse_document_id(document_id) -> UUID:
    """把入参（字符串或 UUID）统一转换为 UUID。

    Args:
        document_id: 文档 ID（str / UUID）。

    Returns:
        规范化后的 UUID。

    Raises:
        DocumentNotFound: ID 格式非法（与「找不到」同处理，避免泄露库内细节）。
    """
    try:
        return document_id if isinstance(document_id, UUID) else UUID(str(document_id))
    except (ValueError, AttributeError, TypeError):
        raise DocumentNotFound(document_id)


async def _load_document(db: AsyncSession, document_id) -> Document:
    """按 ID 读取未软删除的文档。

    Args:
        db: 异步数据库会话。
        document_id: 文档 ID。

    Returns:
        Document 对象。

    Raises:
        DocumentNotFound: 不存在 / 已软删除 / ID 非法。
    """
    doc_uuid = _parse_document_id(document_id)
    doc = (
        await db.execute(
            select(Document).where(Document.id == doc_uuid, Document.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if doc is None:
        raise DocumentNotFound(document_id)
    return doc


def assert_can_index(doc: Document) -> None:
    """校验文档当前状态是否允许入库（供 API 层做前置校验复用）。

    规则：
      1. 必须已审核通过（review_status='approved'）；
      2. 不能正在入库中——除非上次入库已僵死（超过阈值仍停留在 indexing）。

    Args:
        doc: 文档对象。

    Raises:
        IndexingNotAllowed: 未通过审核（含已驳回 / 待审核）。
        IndexingInProgress: 确实正在入库中。
    """
    if doc.status == "indexing":
        # 判断是否为僵死任务：updated_at 距今超过阈值则视为可重跑
        updated = doc.updated_at
        if updated is None:
            # 没有更新时间戳时保守处理：视为僵死，允许重跑（避免永久卡死）
            return
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated > timedelta(minutes=INDEXING_STALE_MINUTES):
            logger.warning(
                "文档 %s 停留在 indexing 超过 %d 分钟，判定为僵死任务，允许重新入库",
                doc.id, INDEXING_STALE_MINUTES,
            )
            return
        raise IndexingInProgress()

    # 只有审核通过的文档才允许入库（驳回/待审核一律拒绝）
    if doc.review_status != "approved":
        raise IndexingNotAllowed(
            f"文档尚未审核通过（当前审核结论：{doc.review_status}），不能入库"
        )


async def _replace_chunks(
    db: AsyncSession, doc: Document, chunks: List[Chunk], vectors: List[List[float]]
) -> int:
    """用新切片**整体替换**该文档的旧切片（保证重入库幂等）。

    Args:
        db: 异步数据库会话。
        doc: 文档对象。
        chunks: 切片列表（含 heading 等元信息）。
        vectors: 与 chunks 一一对应的向量。

    Returns:
        写入的切片条数。
    """
    # 1) 先删除旧切片：重入库时避免新旧切片混杂导致检索结果重复
    await db.execute(delete(DocumentChunk).where(DocumentChunk.doc_id == doc.id))

    # 2) 批量写入新切片
    rows = []
    for chunk, vector in zip(chunks, vectors):
        rows.append(
            DocumentChunk(
                doc_id=doc.id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=vector,
                # metadata：供 P4 引用卡片展示「来自哪篇文档的哪个章节」
                metadata_json={
                    "filename": doc.filename,
                    "file_type": doc.file_type,
                    "heading": chunk.heading or "",
                },
            )
        )
    db.add_all(rows)
    # 先 flush 拿到行数即可，事务由上层 index_document 统一提交
    await db.flush()
    return len(rows)


async def index_document(document_id) -> int:
    """执行文档入库（解析 → 切片 → 向量化 → 写库），返回切片数。

    本函数是 Celery 任务与进程内回退路径的**共同入口**，
    内部自带会话与事务管理，失败时把文档状态回退为 'approved' 并记录原因。

    Args:
        document_id: 文档 ID（str / UUID）。

    Returns:
        成功写入的切片条数。

    Raises:
        DocumentNotFound: 文档不存在 / 已删除 / ID 非法。
        IndexingNotAllowed / IndexingInProgress: 状态不允许入库。
        DocumentParseError / EmbeddingError / StorageError: 管线各环节失败
            （状态已回退，异常继续向上抛以便 worker 记录失败）。
    """
    settings = get_settings()
    storage = build_storage()

    async with SessionLocal() as db:
        # ① 读取文档并校验状态（越早失败越省资源）
        doc = await _load_document(db, document_id)
        assert_can_index(doc)

        # ② 标记为入库中（先落盘，便于前端展示「入库中」）
        doc.status = "indexing"
        doc.last_index_error = None
        await db.commit()

        try:
            # ③ 读原始文件 → 解析 → 切片
            content = await storage.get_object(doc.original_path)
            blocks = parse_document(content, doc.file_type)
            chunks = chunk_blocks(blocks, chunk_size=settings.CHUNK_SIZE, overlap=settings.CHUNK_OVERLAP)

            if not chunks:
                raise DocumentParseError("文档切片结果为空，无法入库")
            if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
                # 截断而非无限向量化：控制费用与耗时，并在文档中记录提示
                logger.warning(
                    "文档 %s 切片数 %d 超过上限 %d，仅入库前 %d 片",
                    doc.id, len(chunks), MAX_CHUNKS_PER_DOCUMENT, MAX_CHUNKS_PER_DOCUMENT,
                )
                chunks = chunks[:MAX_CHUNKS_PER_DOCUMENT]

            # ④ 批量向量化
            vectors = await embedding_mod.embed_texts([c.content for c in chunks])

            # ⑤ 替换切片并 ⑥ 更新文档状态
            written = await _replace_chunks(db, doc, chunks, vectors)
            doc.status = "indexed"
            doc.chunk_count = written
            doc.indexed_at = datetime.now(timezone.utc)
            doc.last_index_error = None
            await db.commit()

            logger.info(
                "文档入库成功：id=%s，filename=%s，切片数=%d",
                doc.id, doc.filename, written,
            )
            return written

        except (DocumentParseError, embedding_mod.EmbeddingError, StorageError) as exc:
            # 可预期的管线失败：回滚并把状态恢复为「已审核未入库」，保留错误原因
            await db.rollback()
            doc = await _load_document(db, document_id)
            doc.status = "approved"
            doc.last_index_error = str(exc)[:500]
            await db.commit()
            logger.error("文档入库失败（已回退状态）：id=%s，原因=%s", doc.id, exc)
            raise
        except Exception as exc:  # noqa: BLE001 —— 兜底：任何异常都不让文档卡在 indexing
            await db.rollback()
            doc = await _load_document(db, document_id)
            doc.status = "approved"
            doc.last_index_error = f"入库异常：{exc}"[:500]
            await db.commit()
            logger.exception("文档入库发生未知异常（已回退状态）：id=%s", doc.id)
            raise


async def unpublish_document(document_id) -> None:
    """文档下线：删除全部向量切片，使其不再参与检索。

    下线后文档状态回到 'approved'（已审核、未入库），
    审核结论 review_status 保持 approved 不变（审核与入库解耦）；
    之后可再次调用 index_document 重新入库。

    Args:
        document_id: 文档 ID（str / UUID）。

    Raises:
        DocumentNotFound: 文档不存在 / 已删除 / ID 非法。
        IndexingInProgress: 正在入库中，不能下线（避免与入库任务并发冲突）。
    """
    async with SessionLocal() as db:
        doc = await _load_document(db, document_id)
        if doc.status == "indexing":
            raise IndexingInProgress("文档正在入库中，请入库完成后再下线")

        # 删除切片：document_chunks 的 FK 为 ON DELETE CASCADE，
        # 这里显式删除以便在「不删文档」的前提下清理向量。
        await db.execute(delete(DocumentChunk).where(DocumentChunk.doc_id == doc.id))
        doc.status = "approved"  # 回到「已审核未入库」
        doc.chunk_count = 0
        doc.indexed_at = None
        doc.last_index_error = None
        await db.commit()

        logger.info("文档已下线：id=%s，filename=%s", doc.id, doc.filename)


def _broker_reachable(broker_url: str, timeout: float = 0.4) -> bool:
    """快速探测 Celery broker（Redis）是否可达（用于决定是否投递异步任务）。

    说明：直接调用 Celery 的 .delay() 在 broker 不可达时会阻塞重试，
          可能把 HTTP 请求拖住；这里先用一次短超时的 TCP 探测做前置判断，
          不可达则回退为进程内执行，保证接口响应时间可控。

    Args:
        broker_url: Celery broker URL（形如 redis://host:6379/0）。
        timeout: 探测超时（秒）。

    Returns:
        可达返回 True，否则 False（解析失败也按不可达处理）。
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse(broker_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
        import socket

        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 —— 探测失败一律按不可达处理
        return False


def enqueue_index_document(document_id) -> bool:
    """把入库任务投递到 Celery；不可用时返回 False 由调用方回退。

    Args:
        document_id: 文档 ID。

    Returns:
        True 表示已成功投递到 Celery（由 worker 异步执行）；
        False 表示未投递（未启用 Celery / Redis 不可达 / celery 未安装），
        调用方应改用 FastAPI BackgroundTasks 在进程内执行。
    """
    settings = get_settings()
    if not settings.USE_CELERY:
        return False
    # 先做一次快速可达性探测，避免不可达时 kombu 长时间阻塞
    if not _broker_reachable(settings.CELERY_BROKER_URL):
        logger.warning(
            "Celery broker 不可达（%s），回退为进程内执行入库", settings.CELERY_BROKER_URL
        )
        return False

    try:
        # 懒导入：celery 未安装或无 broker 时不影响主流程
        from ..tasks.indexing_tasks import index_document_task

        index_document_task.delay(str(document_id))
        logger.info("已投递入库任务到 Celery：document_id=%s", document_id)
        return True
    except Exception as exc:  # noqa: BLE001 —— 投递失败不影响主流程，回退即可
        logger.warning("投递 Celery 任务失败，回退为进程内执行：%s", exc)
        return False
