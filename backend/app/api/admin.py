# /admin 管理员审核接口（P3-B1）
# ---------------------------------------------------------------------
# 职责：为管理员后台（P3-F1）提供「审核队列 / 审核动作 / 文档预览」三类能力。
#
# 接口契约（与前端 src/services/adminApi.ts、src/types.ts 严格对齐）：
#   GET  /admin/documents                  审核队列（可按审核结论/文件名筛选，支持分页）
#   POST /admin/documents/{id}/review      通过 / 驳回（驳回必填理由）
#   GET  /admin/documents/{id}/preview     文档文本预览（管理员判断是否可入库）
#   POST /admin/documents/{id}/index       重新入库（P3-B2：重跑解析→切片→向量化）
#   POST /admin/documents/{id}/unpublish   下线（删除向量切片，不再参与检索）
#
# P3-B2 联动：审核「通过」后自动触发入库任务（异步投递，见 _dispatch_indexing），
#             满足《需求清单》「审核通过即触发入库」的链路约定。
#
# 鉴权（RBAC）：
#   整个路由集合挂载 require_admin 依赖（router 级 dependencies），
#   因此**任何** /admin/* 请求都先过管理员校验：
#     未登录 / 令牌失效 → 401（UnauthorizedError）
#     已登录但非 admin  → 403（ForbiddenError，见 core/security.require_admin）
#   这满足《需求清单》P3 功能1「RBAC 校验仅 admin 可操作」。
#
# 安全约定：
#   - 审核动作只接受 action，不接受任意 status/review_status 值，避免状态被随意篡改；
#   - 不返回可拼接的下载直链，预览内容一律经本接口鉴权后下发。

from typing import Dict, List, Optional
from uuid import UUID

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import (
    DocumentNotFound,
    IndexingInProgress,
    IndexingNotAllowed,
    ReviewNotAllowed,
)
from ..core.logging import get_logger
from ..core.security import require_admin
from ..db.engine import get_db
from ..db.models import Document, User
from ..schemas import (
    AdminDocumentListResponse,
    AdminDocumentOut,
    DocumentPreviewResponse,
    IndexResult,
    ReviewRequest,
    ReviewResult,
)
from ..services import indexing_service, review_service
from ..services.storage import BaseStorage, StorageError
# 复用 /documents 中已定义的存储依赖，保证两种场景用同一套后端构造逻辑
from .documents import get_storage_dep

logger = get_logger(__name__)

# 路由集合：dependencies 使本集合下**所有**接口强制要求管理员身份
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


async def _collect_usernames(db: AsyncSession, docs: List[Document]) -> Dict[UUID, str]:
    """批量取出文档相关的用户名（提交人 + 审核人），避免逐行查库（N+1）。

    Args:
        db: 异步数据库会话。
        docs: 文档列表。

    Returns:
        {user_id: username} 映射；用户已注销则不包含对应键。
    """
    # 汇总本页涉及的提交人与审核人 ID（去重并排除空值）
    user_ids = {d.user_id for d in docs if d.user_id} | {d.reviewer_id for d in docs if d.reviewer_id}
    if not user_ids:
        return {}

    from sqlalchemy import select

    rows = (await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))).all()
    return {row.id: row.username for row in rows}


def _to_out(doc: Document, usernames: Dict[UUID, str]) -> AdminDocumentOut:
    """把 ORM 文档对象转换为管理员视角的响应模型。

    Args:
        doc: 文档 ORM 对象。
        usernames: {user_id: username} 映射（由 _collect_usernames 产出）。

    Returns:
        AdminDocumentOut：含提交人/审核人的用户名（脱敏，不暴露 ID 之外的信息）。
    """
    return AdminDocumentOut(
        id=str(doc.id),
        filename=doc.filename,
        file_type=doc.file_type,
        status=doc.status,
        review_status=doc.review_status,
        size_bytes=doc.size_bytes or 0,
        uploader=usernames.get(doc.user_id) if doc.user_id else None,
        reviewer=usernames.get(doc.reviewer_id) if doc.reviewer_id else None,
        review_note=doc.review_note,
        reviewed_at=doc.reviewed_at.isoformat() if doc.reviewed_at else None,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        # P3-B2 入库状态：切片数 / 入库时间 / 最近一次失败原因
        chunk_count=doc.chunk_count or 0,
        indexed_at=doc.indexed_at.isoformat() if doc.indexed_at else None,
        last_index_error=doc.last_index_error,
    )


# 进程内后台任务的强引用集合：
# asyncio 只保留弱引用，若不强引用，任务可能在 GC 时被静默取消（尤其是耗时入库任务）。
# 任务完成后由 add_done_callback 自动移除，不会无限增长。
_BACKGROUND_TASKS: set = set()


async def _run_indexing_async(document_id: str) -> None:
    """在**当前事件循环**中异步执行入库（Celery 不可用时的回退路径）。

    ⚠️ 为什么不用 asyncio.run 另起循环：
       入库用的是全局异步引擎（app.db.engine.engine），其连接池中的 Future
       绑定在创建它的事件循环上。若在新循环里复用同一引擎，会触发
       「Future attached to a different loop」错误。因此回退路径必须与 Web
       请求共用同一个事件循环，这也是这里使用 asyncio.create_task 的原因。

    异常一律吞掉并记日志：失败原因已由服务层写入 documents.last_index_error。

    Args:
        document_id: 文档 ID 字符串。
    """
    try:
        await indexing_service.index_document(document_id)
    except Exception as exc:  # noqa: BLE001 —— 后台任务：记录即可（状态已由服务层回退）
        logger.error("进程内入库任务失败：document_id=%s，原因=%s", document_id, exc)


def _dispatch_indexing(document_id) -> bool:
    """触发文档入库：优先投递 Celery，不可用时回退为进程内后台任务。

    Args:
        document_id: 文档 ID。

    Returns:
        True 表示已投递 Celery（异步）；False 表示走进程内后台任务。
    """
    # 1) 优先走 Celery（生产路径：独立 worker 执行，不占用 Web 进程）
    if indexing_service.enqueue_index_document(document_id):
        return True

    # 2) 回退：在同一事件循环里创建后台任务，请求可立即返回
    task = asyncio.create_task(_run_indexing_async(str(document_id)))
    _BACKGROUND_TASKS.add(task)
    # 任务结束后从集合中移除，避免集合无限增长
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    logger.info("Celery 不可用，已创建进程内入库任务：document_id=%s", document_id)
    return False


@router.get(
    "/documents",
    response_model=AdminDocumentListResponse,
    summary="审核队列（管理员）",
    description=(
        "分页返回文档列表，默认按提交时间倒序。"
        "可用 review_status 筛选待审核/已通过/已驳回，用 keyword 模糊匹配文件名。"
    ),
)
async def list_documents(
    # 审核结论筛选：不传则返回全部（前端默认传 pending 看待办）
    review_status: Optional[str] = Query(
        default=None, description="审核结论筛选：pending / approved / rejected"
    ),
    # 文件名关键字：大小写不敏感模糊匹配
    keyword: Optional[str] = Query(default=None, description="文件名关键字（模糊匹配）"),
    limit: Optional[int] = Query(default=None, ge=1, le=100, description="每页条数（1~100）"),
    offset: Optional[int] = Query(default=None, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    # 管理员身份由 router 级依赖保证；此处的 admin 仅用于审计日志
    admin: User = Depends(require_admin),
) -> AdminDocumentListResponse:
    """查询审核队列。

    Raises:
        HTTPException 409: review_status 取值非法（由 ReviewNotAllowed 转换）。
    """
    try:
        docs, total = await review_service.list_review_queue(
            db=db, review_status=review_status, keyword=keyword, limit=limit, offset=offset
        )
    except ReviewNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc

    usernames = await _collect_usernames(db, docs)
    real_limit, real_offset = review_service.normalize_pagination(limit, offset)
    logger.info("管理员 %s 查询审核队列：返回 %d/%d 条", admin.username, len(docs), total)

    return AdminDocumentListResponse(
        items=[_to_out(d, usernames) for d in docs],
        total=total,
        limit=real_limit,
        offset=real_offset,
    )


@router.post(
    "/documents/{document_id}/review",
    response_model=ReviewResult,
    summary="审核文档（通过 / 驳回）",
    description="通过或驳回一篇文档；驳回时必须填写理由（服务端强制校验）。",
)
async def review_document(
    document_id: str,
    payload: ReviewRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ReviewResult:
    """执行审核动作并回传更新后的文档条目。

    P3-B2 联动：审核「通过」后自动触发入库任务（异步），
    失败不影响审核结果本身——入库失败会写入 last_index_error，可重新入库。

    Raises:
        HTTPException 404: 文档不存在或已被删除。
        HTTPException 409: 状态冲突（已通过）或驳回未填理由。
    """
    try:
        # 1) 取文档（不存在/已删除 → DocumentNotFound）
        doc = await review_service.get_document_for_review(db, document_id)
        # 2) 执行审核（含状态机与驳回理由校验）
        updated = await review_service.review_document(
            db=db, doc=doc, reviewer=admin, action=payload.action, note=payload.note
        )
    except DocumentNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ReviewNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc

    # 审核人用户名直接取自当前管理员，无需再查库
    usernames = {admin.id: admin.username}
    if updated.user_id and updated.user_id not in usernames:
        # 提交人用户名仍需查一次（仅单条，无 N+1 问题）
        usernames.update(await _collect_usernames(db, [updated]))

    logger.info(
        "管理员 %s 审核文档 %s：%s", admin.username, document_id, payload.action
    )

    # P3-B2：审核通过即触发入库（「审核通过 → 入库」链路的关键衔接点）。
    # 投递失败（无 Redis）时由进程内后台任务兜底，保证链路不断。
    if payload.action == "approved":
        queued = _dispatch_indexing(updated.id)
        logger.info(
            "文档 %s 审核通过，已触发入库（%s）",
            document_id,
            "Celery 异步" if queued else "进程内后台任务",
        )

    return ReviewResult(document=_to_out(updated, usernames))


@router.post(
    "/documents/{document_id}/index",
    response_model=IndexResult,
    summary="重新入库（管理员）",
    description=(
        "对已审核通过的文档重跑入库管线（解析 → 切片 → 向量化 → 写向量）。"
        "适用于入库失败后重试、或文档内容更新后重建索引。"
    ),
)
async def index_document(
    document_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> IndexResult:
    """触发文档入库 / 重入库。

    说明：入库是重操作，这里**只做校验并投递任务**，不等待执行完成；
          执行结果通过文档的 status / chunk_count / last_index_error 体现。

    Raises:
        HTTPException 404: 文档不存在或已被删除。
        HTTPException 409: 未审核通过不允许入库；或正在入库中。
    """
    try:
        doc = await review_service.get_document_for_review(db, document_id)
        # 前置校验：复用服务层的状态机判断（未通过审核 / 正在入库都会抛错）
        indexing_service.assert_can_index(doc)
    except DocumentNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except (IndexingNotAllowed, IndexingInProgress) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc

    # 只做校验 + 投递：状态由服务层统一置为 indexing。
    # ⚠️ 这里**不预先**把 status 改成 indexing —— 否则服务层进入管线时
    #    会认为「已有入库任务在跑」而自我拒绝（IndexingInProgress）。
    queued = _dispatch_indexing(doc.id)
    logger.info("管理员 %s 触发文档入库：%s", admin.username, document_id)

    return IndexResult(
        document=_to_out(doc, {}),
        queued=queued,
        message="已提交入库任务（解析 → 切片 → 向量化）" if queued else "已在进程内开始入库",
    )


@router.post(
    "/documents/{document_id}/unpublish",
    response_model=IndexResult,
    summary="文档下线（管理员）",
    description="删除该文档的全部向量切片，使其不再参与检索；审核结论保持不变，可再次入库。",
)
async def unpublish_document(
    document_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> IndexResult:
    """下线文档（清理向量切片）。

    下线是同步操作（只是一次 DELETE，耗时可控），因此直接执行并回传结果。

    Raises:
        HTTPException 404: 文档不存在或已被删除。
        HTTPException 409: 正在入库中，不能下线。
    """
    try:
        await indexing_service.unpublish_document(document_id)
    except DocumentNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except IndexingInProgress as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc

    # 重新读取以回传最新状态（含 chunk_count=0 / indexed_at=None）
    doc = await review_service.get_document_for_review(db, document_id)
    logger.info("管理员 %s 下线文档 %s", admin.username, document_id)

    return IndexResult(
        document=_to_out(doc, {}),
        queued=False,
        message="已下线，向量切片已清理（可重新入库）",
    )


@router.get(
    "/documents/{document_id}/preview",
    response_model=DocumentPreviewResponse,
    summary="文档文本预览（管理员）",
    description="读取原始文件并提取纯文本供管理员预览；超长自动截断。",
)
async def preview_document(
    document_id: str,
    db: AsyncSession = Depends(get_db),
    storage: BaseStorage = Depends(get_storage_dep),
    admin: User = Depends(require_admin),
) -> DocumentPreviewResponse:
    """生成文档预览文本。

    说明：预览失败（如原始文件已被清理、旧版 .doc 无法解析）不抛错，
    而是在 previewable=False 时返回可读提示，保证审核流程不被阻塞。

    Raises:
        HTTPException 404: 文档不存在或已被删除。
    """
    try:
        doc = await review_service.get_document_for_review(db, document_id)
    except DocumentNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc

    try:
        text, truncated = await review_service.preview_document(storage=storage, doc=doc)
        # 提取成功（即使内容为空也算成功）；无法解析时服务层返回可读提示文本
        previewable = True
    except StorageError as exc:
        # 原始文件缺失/不可读：明确告知管理员，但不算文档不存在（元数据仍在）
        logger.warning("文档预览失败（存储读取错误）：id=%s，%s", document_id, exc)
        text = f"（原始文件读取失败：{exc}）"
        truncated = False
        previewable = False

    logger.info("管理员 %s 预览文档 %s", admin.username, document_id)
    return DocumentPreviewResponse(
        id=str(doc.id),
        filename=doc.filename,
        file_type=doc.file_type,
        text=text,
        truncated=truncated,
        previewable=previewable,
    )
