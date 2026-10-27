# 文档上传服务（P1-B2）
# ---------------------------------------------------------------------
# 职责：把「一次上传请求」编排为「校验 → 写对象存储 → 落库元数据」的完整流程，
#       并保证失败时可追溯、不产生孤儿文件（见《云存储测试方案》风险表 S5 / S12）。
#
# 关键约束（来自《开发任务清单》P1-B2 与《云存储测试方案》）：
#   1. 格式白名单 + 大小校验，且**在写存储之前**完成（非法文件绝不落盘）。
#   2. 先写存储后写 DB；若 DB 写入失败，补偿删除已上传对象，避免「DB 无记录但存储有文件」的孤儿。
#   3. 二进制不进库：数据库只存 original_path（相对 key）与元数据。

from typing import Optional, Tuple
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.logging import get_logger
from app.db.models import Document
from app.services import storage as storage_mod

logger = get_logger(__name__)

# 危险扩展名：用于「双扩展名绕过」防护（见《云存储测试方案》ST-U04）。
# 形如 evil.exe.docx 的文件，最终扩展名 .docx 合法，但其内部含 .exe，
# 直接按最终扩展名放行存在风险，故任意分段命中此集合即拒绝。
_DANGEROUS_EXTENSIONS = {
    "exe", "bat", "cmd", "com", "scr", "vbs", "js", "jse", "wsf", "wsh",
    "msi", "msp", "ps1", "sh", "bash", "jar", "php", "py", "rb", "dll",
    "scr", "pif", "reg", "lnk",
}


class UploadValidationError(Exception):
    """上传校验失败异常：携带 HTTP 状态码与面向用户的提示文案。

    由 api/documents.py 捕获并转换为对应 HTTPException（不落存储、不落库）。
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _derive_extension(filename: str) -> str:
    """从文件名提取小写扩展名（不含点）。

    例：'报告.FINAL.DOCX' → 'docx'；'a.b.exe.docx' → 'docx'（取最后一段）。
    """
    # 仅取最后一个点之后的部分，并去掉可能的查询串/路径残留
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


def _derive_doc_type(ext: str) -> str:
    """根据扩展名推导文档类型（与前端 DocType 对齐）。

    doc / docx → 'word'；md → 'markdown'。仅白名单内扩展名会进入本函数。
    """
    return "word" if ext in ("doc", "docx") else "markdown"


def validate_upload(
    filename: Optional[str],
    size: int,
    client_type_hint: Optional[str] = None,
) -> Tuple[str, str]:
    """上传前校验：格式白名单 + 双扩展名防护 + 空文件 + 大小上限。

    所有不合法情况都抛出 UploadValidationError（含合适的 HTTP 状态码），
    调用方据此在「写存储之前」就拒绝请求，保证存储里不出现半截/非法对象。

    Args:
        filename: 原始文件名（来自 UploadFile.filename）。
        size: 文件字节数（来自 await file.read() 后的 len）。
        client_type_hint: 前端附带的 type 提示（word/markdown），用于二次校验。

    Returns:
        (ext, doc_type) 二元组，ext 为不含点的小写扩展名，doc_type 为 'word'/'markdown'。

    Raises:
        UploadValidationError: 任一校验不通过。
    """
    settings = get_settings()

    # 1) 文件名为空：拒绝
    if not filename or not filename.strip():
        raise UploadValidationError("文件名不能为空", status_code=400)

    # 2) 提取扩展名；无扩展名直接拒绝（白名单内必有扩展名）
    ext = _derive_extension(filename)
    if not ext:
        raise UploadValidationError(
            f"「{filename}」缺少扩展名，仅支持 .doc / .docx / .md", status_code=415
        )

    # 3) 双扩展名 / 危险扩展名防护：文件名任意分段命中危险集合即拒绝
    segments = filename.lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1].split(".")
    if any(seg in _DANGEROUS_EXTENSIONS for seg in segments):
        raise UploadValidationError(
            f"「{filename}」包含不允许的扩展名，存在安全风险", status_code=400
        )

    # 4) 扩展名白名单（大小写不敏感）：不在名单内拒绝
    if ext not in settings.allowed_extensions_list:
        raise UploadValidationError(
            f"「{filename}」格式不支持，仅可上传 {', '.join('.' + e for e in settings.allowed_extensions_list)}",
            status_code=415,
        )

    # 5) 空文件：0 字节没有入库价值，且会让后续解析阶段报错
    if size == 0:
        raise UploadValidationError(f"「{filename}」是空文件，无法上传", status_code=400)

    # 6) 大小上限：超出返回 413（与前端 parseErrorMessage 的 413 分支对应）
    if size > settings.MAX_UPLOAD_SIZE:
        raise UploadValidationError(
            f"「{filename}」超过 {settings.max_upload_size_mb}MB 上限（实际 {size / 1024 / 1024:.1f}MB）",
            status_code=413,
        )

    # 7) 前端 type 提示二次校验：若提供且与扩展名推导不一致，视为异常（防篡改/误传）
    doc_type = _derive_doc_type(ext)
    if client_type_hint and client_type_hint.strip() and client_type_hint.strip() != doc_type:
        raise UploadValidationError(
            f"文件类型与扩展名不符（声明 {client_type_hint}，实际 {doc_type}）", status_code=400
        )

    return ext, doc_type


def _parse_user_id(user_id: Optional[str]) -> Optional[UUID]:
    """解析可选的 user_id 为 UUID；非法或为空返回 None。

    P1 阶段尚未接入登录鉴权，前端不传 user_id，此处得到 None（文档表 user_id 在 P1 允许为空）。
    P2-B2 鉴权上线后，user_id 由令牌解析得到，必然有效。
    """
    if not user_id or not user_id.strip():
        return None
    try:
        return UUID(user_id.strip())
    except (ValueError, AttributeError):
        # 非法 UUID（如测试字符串）按匿名处理，不阻断上传、不报错
        logger.warning("收到非法 user_id=%r，按匿名处理", user_id)
        return None


async def upload_document(
    db: AsyncSession,
    storage: storage_mod.BaseStorage,
    file: UploadFile,
    client_type: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Document:
    """执行一次完整上传：读文件 → 校验 → 写存储 → 落库。

    流程与失败处理（对应《云存储测试方案》风险表）：
      - 校验（含格式/大小/空文件）在读取后、写存储前完成，非法请求不落盘。
      - 先 put_object 再写 DB；若 DB 失败，补偿 delete_object 删除已上传对象，杜绝孤儿文件。
      - 存储自身失败（StorageError）直接上抛，此时尚未写 DB，无脏记录。

    Args:
        db: 异步数据库会话（FastAPI Depends(get_db)）。
        storage: 存储后端实例（便于测试时注入本地/内存实现）。
        file: 上传的文件对象（FastAPI UploadFile）。
        client_type: 前端附带的 type 提示（可选，二次校验用）。
        user_id: 提交人 ID（可选；P1 多为 None）。

    Returns:
        Document：已落库的文档元数据库对象（含生成的 id 与 original_path）。

    Raises:
        UploadValidationError: 校验失败（前置，不落盘）。
        storage_mod.StorageError: 存储写入失败（5xx，不落库）。
        Exception: DB 写入失败已补偿删除对象，转换为 500 级错误。
    """
    # 1) 读取完整文件内容（P1 上限 20MB，内存可接受；后续大文件阶段改流式/分片）
    content = await file.read()
    size = len(content)

    # 2) 前置校验（写存储之前）
    ext, doc_type = validate_upload(file.filename, size, client_type)

    # 3) 生成相对 key 并先写对象存储
    key = storage_mod.build_object_key(_parse_user_id(user_id), ext)
    try:
        await storage.put_object(key, content, file.content_type)
    except storage_mod.StorageError:
        # 存储失败：DB 尚未写入，直接上抛（router 转 5xx），保证无脏记录
        raise

    # 4) 落库：写入文档元数据（原始文件不进库，仅存路径与元数据）
    doc = Document(
        user_id=_parse_user_id(user_id),
        filename=file.filename or f"untitled.{ext}",
        original_path=key,  # 相对 key（不含 bucket），便于换存储后端
        file_type=doc_type,
        mime_type=file.content_type or "application/octet-stream",
        size_bytes=size,
        status="pending",  # P1 提交即「待审核」，等待 P3 管理员审核
    )
    try:
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
    except Exception as exc:  # noqa: BLE001 —— 需捕获全部以执行补偿删除
        # 补偿：删除已上传的对象，避免「DB 无记录但存储有文件」的孤儿
        logger.error("文档元数据落库失败，补偿删除已上传对象 key=%s：%s", key, exc)
        try:
            await storage.delete_object(key)
        except Exception as del_exc:  # 补偿删除也失败：记录告警，仍向上抛原错误
            logger.warning("补偿删除对象失败 key=%s：%s", key, del_exc)
        await db.rollback()
        raise storage_mod.StorageError(
            f"文档入库失败，已清理已上传文件：{exc}", status_code=500
        ) from exc

    logger.info(
        "文档上传成功：id=%s，filename=%s，type=%s，size=%d，path=%s",
        doc.id, doc.filename, doc.file_type, doc.size_bytes, doc.original_path,
    )
    return doc


async def list_documents(
    db: AsyncSession,
    user_id: Optional[UUID] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[Document]:
    """（预留）查询文档列表：按用户/状态过滤，按创建时间倒序。

    说明：P1-B2 仅交付上传接口；此函数供后续审核列表（P3）与历史查看复用，
    不在此阶段暴露为独立接口，故无对应路由。
    """
    stmt = select(Document)
    if user_id is not None:
        stmt = stmt.where(Document.user_id == user_id)
    if status is not None:
        stmt = stmt.where(Document.status == status)
    # 过滤软删除（deleted_at 为空）
    stmt = stmt.where(Document.deleted_at.is_(None))
    stmt = stmt.order_by(Document.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
