# /documents/upload 文档上传接口（P1-B2）
# ---------------------------------------------------------------------
# 职责：接收前端（P1-F3）以 multipart/form-data 提交的 Word / Markdown 文件，
#       经「格式 + 大小 + 空文件」校验后，交由文档上传服务落盘对象存储并写入元数据。
#
# 接口契约（与前端 src/services/documentUpload.ts、src/types.ts 严格对齐）：
#   POST /documents/upload   Content-Type: multipart/form-data
#   表单字段：file（必填，原始文件）、type（可选，word/markdown 提示，用于二次校验）
#   成功 201：DocumentUploadResponse { id, filename, type, status, size, storage_path, created_at }
#   失败：HTTPException，detail 为可读中文（前端 parseErrorMessage 优先取 detail）
#
# 鉴权（P2-B2 / P2-Dep1）：
#   P1 阶段未接入登录，提交人由表单自报 user_id 且通常为空；
#   P2 起所有接口要求鉴权，提交人一律由 JWT 令牌解析，**不再信任表单传入的 user_id**
#   （否则任何人都能伪造他人身份提交文档）。未携带有效令牌直接 401。

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging import get_logger
from ..core.security import get_current_user_id
from ..db.engine import get_db
from ..db.models import Document, DocumentChunk
from ..schemas import (
    ChunkDetailResponse,
    DocumentUploadResponse,
    MyDocumentListResponse,
    MyDocumentOut,
)
from ..services import documents as documents_service
from ..services import embedding as embedding_mod
# P7-B2：审计日志（上传动作留痕）
from ..services import audit_service
from ..services.documents import UploadValidationError
from ..services.storage import BaseStorage, StorageError, build_storage

logger = get_logger(__name__)

# 路由集合；main.py 中以 /documents 相关路径统一注册
router = APIRouter(tags=["documents"])


def get_storage_dep() -> BaseStorage:
    """存储后端依赖：根据配置构造（local / minio）。

    抽成依赖便于测试时通过 FastAPI dependency_overrides 注入本地/内存实现。
    """
    return build_storage()


@router.post(
    "/documents/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传文档（Word / Markdown / 图片 / 音频 / 视频）",
    description=(
        "接收 .doc/.docx/.md 以及图片（jpg/png/webp/bmp/gif）、音频（mp3/wav/m4a/aac/flac/ogg）、"
        "视频（mp4/mov/m4v/avi/mkv/webm）。校验包括：扩展名白名单、危险双扩展名、"
        "按类型分级的大小上限（图片 10MB / 音频 50MB / 文档 20MB / 视频 200MB）、"
        "文件头魔数与扩展名一致性、可选 ClamAV 病毒扫描。"
        "原始文件写入对象存储（MinIO 或本地目录），元数据与路径写入 documents 表，"
        "状态固定为 pending（待审核）。图片/音视频的文本内容在入库阶段经 OCR/ASR 提取。"
    ),
)
async def upload_document(
    # file：原始文件，必须；前端字段名固定为 'file'
    file: UploadFile = File(..., description="原始文件（文档 / 图片 / 音频 / 视频）"),
    # type：前端附带的类型提示（word/markdown/image/audio/video），用于后端二次校验；非必填
    doc_type: Optional[str] = Form(
        None, alias="type", description="类型提示 word/markdown/image/audio/video，用于二次校验"
    ),
    # P2-B2：提交人由 JWT 令牌解析（不再接受表单自报 user_id，杜绝身份伪造）
    current_user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    storage: BaseStorage = Depends(get_storage_dep),
) -> DocumentUploadResponse:
    """处理一次文档上传请求（P1-B2 核心入口）。

    流程：读文件 → 校验（服务内）→ 写对象存储 → 落库（服务内）；
    校验失败在前（不落盘），存储/落库失败有明确错误且不留脏数据。
    """
    try:
        # 委托文档服务完成「校验 → 存储 → 落库」完整编排
        doc = await documents_service.upload_document(
            db=db,
            storage=storage,
            file=file,
            client_type=doc_type,
            # 服务层接收字符串形式的 user_id，这里由已鉴权的 UUID 转换而来
            user_id=str(current_user_id),
        )
    except UploadValidationError as exc:
        # 校验失败（格式/大小/空文件/双扩展名）：直接拒绝，不落盘、不落库
        logger.warning("上传被拒（校验）：%s", exc.message if hasattr(exc, "message") else exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except StorageError as exc:
        # 存储写入失败（5xx）：此时尚未写 DB，无脏记录
        logger.error("上传失败（存储）：%s", exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    # 映射 ORM 对象 → 前端契约响应（字段名与 types.ts 的 DocumentRecord 对齐）
    # P7-B2：上传属于「内容进入知识库」的入口动作，写审计留痕
    # （只记文件名/类型/大小等元信息，绝不记录文件内容）。
    await audit_service.record(
        "document.upload",
        user_id=current_user_id,
        resource_type="document",
        resource_id=str(doc.id),
        detail={
            "filename": doc.filename,
            "file_type": doc.file_type,
            "size_bytes": doc.size_bytes,
        },
    )
    return DocumentUploadResponse(
        id=str(doc.id),
        filename=doc.filename,
        type=doc.file_type,        # 后端 file_type（word/markdown）→ 前端 type
        status=doc.status,
        size=doc.size_bytes or 0,  # 后端 size_bytes → 前端 size
        storage_path=doc.original_path,  # 后端 original_path → 前端 storage_path（相对 key）
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.get(
    "/documents",
    response_model=MyDocumentListResponse,
    summary="我的文档列表（含入库状态）",
    description=(
        "返回当前登录用户提交的文档，按提交时间倒序。"
        "status=indexed 即「已入库」，P3-F1 前端据此展示状态标签；"
        "被驳回时 review_note 带上驳回理由，便于用户修改后重新提交。"
    ),
)
async def list_my_documents(
    limit: Optional[int] = Query(default=50, ge=1, le=100, description="返回条数（1~100）"),
    # P2-B2：提交人一律由令牌解析，只返回本人文档（不提供他人文档查询入口）
    current_user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> MyDocumentListResponse:
    """查询「我的文档」列表（P3-F1 用户侧状态展示的数据源）。

    只返回未软删除、且 user_id 等于当前登录用户的文档，
    天然实现「用户只见自己提交的内容」。
    """
    # 基础过滤：本人 + 未删除
    conditions = [Document.user_id == current_user_id, Document.deleted_at.is_(None)]

    # 总数与列表共用同一套条件，保证分页准确
    total = int(
        (await db.execute(select(func.count()).select_from(Document).where(*conditions))).scalar() or 0
    )
    rows = (
        (
            await db.execute(
                select(Document)
                .where(*conditions)
                .order_by(Document.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return MyDocumentListResponse(
        items=[
            MyDocumentOut(
                id=str(doc.id),
                filename=doc.filename,
                file_type=doc.file_type,
                status=doc.status,
                review_status=doc.review_status,
                size_bytes=doc.size_bytes or 0,
                chunk_count=doc.chunk_count or 0,
                review_note=doc.review_note,
                created_at=doc.created_at.isoformat() if doc.created_at else "",
                indexed_at=doc.indexed_at.isoformat() if doc.indexed_at else None,
                # 入库失败会回退 status 为 approved，故把失败原因一并下发，
                # 前端据此区分「已通过待入库」与「入库失败」
                last_index_error=doc.last_index_error,
                # P4：向量时效性——只有「已入库且有切片」的文档才需要判断：
                # 未入库必然没有向量，提示「向量过期」只会造成困惑。
                embedding_model=doc.embedding_model,
                embedding_stale=bool(doc.chunk_count)
                and embedding_mod.embedding_is_stale(doc.embedding_model),
                # P5：多模态解析元信息（图片/音视频入库后才有值），
                # 用户侧据此看到「音频时长/识别字符数」等，理解为什么检索得到它
                extract_meta=doc.extract_meta,
            )
            for doc in rows
        ],
        total=total,
    )


@router.get(
    "/documents/{document_id}/chunks/{chunk_id}",
    response_model=ChunkDetailResponse,
    summary="引用原文片段（点击引用卡片查看）",
    description=(
        "按 chunk_id 返回知识库切片的完整正文与相邻片段，供答案引用卡片「可跳转」查看原文。"
        "仅对**已入库**（status=indexed）且未删除的文档开放："
        "未审核/已驳回/已下线的文档不会出现在检索结果中，也不应能被单独窥探。"
        "本接口不返回原始文件，不扩大数据暴露面。"
    ),
)
async def get_chunk_detail(
    document_id: str,
    chunk_id: str,
    # P2-B2：要求登录——知识库是共享的，但不对匿名开放
    _current_user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ChunkDetailResponse:
    """返回指定切片的正文、章节与相邻切片 ID（P4-F2 引用跳转的数据源）。

    流程：参数解析 → 文档可用性校验（已入库 + 未删除）→ 取目标切片 →
    取同文档全部切片 ID 序列（用于计算 prev/next 与 total）。

    Args:
        document_id: 文档 ID（UUID 字符串）。
        chunk_id: 切片 ID（UUID 字符串）。
        _current_user_id: 当前登录用户（仅要求登录，不校验归属——共享知识库）。
        db: 数据库会话。

    Returns:
        ChunkDetailResponse。

    Raises:
        HTTPException: 400（ID 非法）/ 404（文档不可见或切片不属于该文档）。
    """
    # 1) ID 格式校验：非法格式与「不存在」同样返回 404，避免泄露库内 ID 规则
    try:
        doc_uuid = UUID(document_id)
        chunk_uuid = UUID(chunk_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="引用片段不存在")

    # 2) 文档可用性：只有已入库且未删除的文档才可被引用查看
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_uuid,
                Document.status == "indexed",
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="引用片段不存在")

    # 3) 取该文档的全部切片 ID（按序号升序）：一次查询同时算出 total 与 prev/next，
    #    避免再为相邻片段单独查两次库
    ordered = (
        (
            await db.execute(
                select(DocumentChunk.id, DocumentChunk.chunk_index)
                .where(DocumentChunk.doc_id == doc_uuid)
                .order_by(DocumentChunk.chunk_index.asc())
            )
        )
        .all()
    )
    ids = [str(row.id) for row in ordered]
    if chunk_id not in ids:
        # 切片不属于该文档（或已被重入库替换）→ 统一 404，不区分细节
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="引用片段不存在")

    # 4) 取目标切片正文与元信息
    target = (
        await db.execute(
            select(DocumentChunk).where(DocumentChunk.id == chunk_uuid)
        )
    ).scalar_one_or_none()
    if target is None:  # 并发下线/重入库导致刚被删除
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="引用片段不存在")

    pos = ids.index(chunk_id)
    meta = target.metadata_json or {}
    return ChunkDetailResponse(
        doc_id=str(doc.id),
        doc_title=doc.filename,
        file_type=doc.file_type,
        chunk_id=str(target.id),
        chunk_index=int(target.chunk_index or 0),
        total_chunks=len(ids),
        heading=str(meta.get("heading") or ""),
        content=target.content or "",
        # 首段无上一段、末段无下一段
        prev_chunk_id=ids[pos - 1] if pos > 0 else None,
        next_chunk_id=ids[pos + 1] if pos + 1 < len(ids) else None,
    )
