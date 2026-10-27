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
# 说明：P1 尚未接入登录鉴权（P2-B2），故 user_id 可选且通常为空；
#      接口本身不做鉴权（鉴权由 P2 统一加中间件），仅保证「未登录也能上传」的 P1 闭环。

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging import get_logger
from ..db.engine import get_db
from ..schemas import DocumentUploadResponse
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
    # user_id：提交人；P1 无鉴权多为空，P2 后由令牌解析得到
    user_id: Optional[str] = Form(None, description="提交人 ID（P1 多为空，P2 鉴权后由令牌解析）"),
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
            user_id=user_id,
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
