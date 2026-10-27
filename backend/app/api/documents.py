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
from ..db.models import Document
from ..schemas import DocumentUploadResponse, MyDocumentListResponse, MyDocumentOut
from ..services import documents as documents_service
from ..services import storage as storage_mod
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
    summary="上传文档（仅 Word / Markdown）",
    description=(
        "接收 .doc/.docx/.md 文件，做格式白名单 + 大小 + 空文件校验，"
        "原始文件写入对象存储（MinIO 或本地目录），元数据与路径写入 documents 表。"
        "返回文档记录，状态固定为 pending（待审核）。"
    ),
)
async def upload_document(
    # file：原始文件，必须；前端字段名固定为 'file'
    file: UploadFile = File(..., description="原始文件（.doc / .docx / .md）"),
    # type：前端附带的类型提示（word/markdown），用于后端二次校验；非必填
    doc_type: Optional[str] = Form(None, alias="type", description="类型提示 word/markdown，用于二次校验"),
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
            )
            for doc in rows
        ],
        total=total,
    )
