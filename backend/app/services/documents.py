# 文档上传服务（P1-B2 / P5 功能1）
# ---------------------------------------------------------------------
# 职责：把「一次上传请求」编排为「校验 → 写对象存储 → 落库元数据」的完整流程，
#       并保证失败时可追溯、不产生孤儿文件（见《云存储测试方案》风险表 S5 / S12）。
#
# 关键约束（来自《开发任务清单》P1-B2 / P5-B1 与《云存储测试方案》）：
#   1. 格式白名单 + 大小分级 + 魔数校验，且**在写存储之前**完成（非法文件绝不落盘）。
#   2. 先写存储后写 DB；若 DB 写入失败，补偿删除已上传对象，避免「DB 无记录但存储有文件」的孤儿。
#   3. 二进制不进库：数据库只存 original_path（相对 key）与元数据。
#   4. P5：音视频体积可达 200MB，因此**先流式落到临时文件**再上传，
#      不再像 P1 那样把整个文件读进内存（否则并发上传几个视频就会把内存打满）。
#   5. P5：可选 ClamAV 病毒扫描（见 services/virus_scan.py），默认关闭。

import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.logging import get_logger
from app.db.models import Document
from app.services import media_types, virus_scan
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

# 流式接收上传文件时的单块大小：1MB 在「内存占用」与「系统调用次数」之间较平衡
_UPLOAD_CHUNK = 1024 * 1024


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


def _derive_doc_type(ext: str) -> Optional[str]:
    """根据扩展名推导文档类型（与前端 DocType 对齐）。

    P1 只需区分 word / markdown；P5 起新增 image / audio / video，
    统一交给 services/media_types.resolve_file_type 判定，避免两处维护映射表。

    Args:
        ext: 不含点的小写扩展名。

    Returns:
        文档类型；不在白名单内时返回 None。
    """
    return media_types.resolve_file_type(ext)


def validate_upload(
    filename: Optional[str],
    size: int,
    client_type_hint: Optional[str] = None,
    head: Optional[bytes] = None,
) -> Tuple[str, str]:
    """上传前校验：格式白名单 + 双扩展名防护 + 空文件 + 大小分级 + 魔数校验。

    所有不合法情况都抛出 UploadValidationError（含合适的 HTTP 状态码），
    调用方据此在「写存储之前」就拒绝请求，保证存储里不出现半截/非法对象。

    Args:
        filename: 原始文件名（来自 UploadFile.filename）。
        size: 文件字节数。
        client_type_hint: 前端附带的 type 提示（word/markdown/image/audio/video），用于二次校验。
        head: 文件开头字节（≥32 字节时启用魔数校验）；None 表示跳过魔数校验
            （P1 的纯校验单测不提供文件内容，此时只校验扩展名与大小）。

    Returns:
        (ext, doc_type) 二元组，ext 为不含点的小写扩展名，doc_type 为文档类型。

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
            f"「{filename}」缺少扩展名，仅支持 {', '.join('.' + e for e in settings.allowed_extensions_list)}",
            status_code=415,
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

    # 6) 类型推导（白名单内必然可推导，防御性判断避免映射表漏项时静默通过）
    doc_type = _derive_doc_type(ext)
    if doc_type is None:
        raise UploadValidationError(
            f"「{filename}」格式不支持（无法识别为文档/图片/音频/视频）", status_code=415
        )

    # 7) 大小上限：按类型分级（图片 10MB / 音频 50MB / 视频 200MB / 文档 20MB）
    #    超出返回 413（与前端 parseErrorMessage 的 413 分支对应）
    size_cap = settings.max_upload_size_for(doc_type)
    if size > size_cap:
        raise UploadValidationError(
            f"「{filename}」超过 {settings.max_upload_size_mb_for(doc_type)}MB 上限"
            f"（实际 {size / 1024 / 1024:.1f}MB）",
            status_code=413,
        )

    # 8) 魔数校验：确认「扩展名」与「真实文件头」一致，挡住改名伪装
    #    说明：文档类（docx/md）不走魔数校验（docx 本质是 zip，判定意义有限），
    #    由 media_types.verify_magic 返回 None 表示「不适用」。
    if head and settings.MEDIA_VERIFY_MAGIC:
        verdict = media_types.verify_magic(head, ext)
        if verdict is False:
            raise UploadValidationError(
                f"「{filename}」的文件内容与扩展名不符（可能被改名伪装），已拒绝上传",
                status_code=415,
            )

    # 9) 前端 type 提示二次校验：若提供且与扩展名推导不一致，视为异常（防篡改/误传）
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


async def _spool_upload(file: UploadFile, cap: int, filename: str) -> Tuple[Path, int, bytes]:
    """把上传流**流式**写入临时文件，边写边计数（超限立即中止）。

    为什么需要它：P5 起支持视频（上限 200MB），若沿用 P1 的 `await file.read()`
    会把整个文件读进内存，几个并发上传就能把服务打挂。这里改为按 1MB 分块落盘，
    同时只保留文件头 32 字节用于魔数校验。

    Args:
        file: FastAPI 的 UploadFile（底层是 SpooledTemporaryFile，大文件已自动落盘）。
        cap: 该类型的体积上限（字节），超过即中止。
        filename: 原始文件名（仅用于错误提示）。

    Returns:
        (临时文件路径, 实际字节数, 文件头字节)。

    Raises:
        UploadValidationError: 体积超过 cap（413）。
    """
    fd, tmp_name = tempfile.mkstemp(prefix="knowagent_upload_", suffix=".part")
    path = Path(tmp_name)
    total = 0
    head = b""
    try:
        # os.fdopen 接管 fd，with 结束即关闭；异常时由外层 unlink
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                # 超限即刻中止，避免恶意用户用超大文件耗尽磁盘
                if total > cap:
                    raise UploadValidationError(
                        f"「{filename}」超过 {round(cap / 1024 / 1024, 2)}MB 上限，已中止上传",
                        status_code=413,
                    )
                # 只保留前 MAGIC_HEAD_BYTES 字节用于魔数判定
                if len(head) < media_types.MAGIC_HEAD_BYTES:
                    head += chunk[: media_types.MAGIC_HEAD_BYTES - len(head)]
                out.write(chunk)
    except BaseException:
        # 任何异常（含校验超限）都要清掉半截临时文件
        path.unlink(missing_ok=True)
        raise
    return path, total, head


async def upload_document(
    db: AsyncSession,
    storage: storage_mod.BaseStorage,
    file: UploadFile,
    client_type: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Document:
    """执行一次完整上传：流式接收 → 校验 → 病毒扫描 → 写存储 → 落库。

    流程与失败处理（对应《云存储测试方案》风险表）：
      - 先按扩展名确定体积上限并流式落临时文件（超限即中止，不占内存）；
      - 校验（格式/大小/魔数/空文件）在写对象存储前完成，非法请求不落盘；
      - 可选 ClamAV 扫描（fail-closed），检出即拒收；
      - 先 put 再写 DB；若 DB 失败，补偿 delete_object 删除已上传对象，杜绝孤儿文件；
      - 无论成功失败，临时文件都会被清理。

    Args:
        db: 异步数据库会话（FastAPI Depends(get_db)）。
        storage: 存储后端实例（便于测试时注入本地/内存实现）。
        file: 上传的文件对象（FastAPI UploadFile）。
        client_type: 前端附带的 type 提示（可选，二次校验用）。
        user_id: 提交人 ID（可选；本地关闭鉴权时可能为 None）。

    Returns:
        Document：已落库的文档元数据库对象（含生成的 id 与 original_path）。

    Raises:
        UploadValidationError: 校验失败（前置，不落盘）。
        virus_scan.VirusScanError: 检出病毒或扫描服务不可用（不落盘）。
        storage_mod.StorageError: 存储写入失败（5xx，不落库）。
    """
    settings = get_settings()
    filename = file.filename or ""

    # 1) 先按扩展名确定「该类型的体积上限」，再开始接收
    #    （未知扩展名用文档上限兜底，随后 validate_upload 会以 415 拒绝）
    ext_guess = _derive_extension(filename)
    type_guess = _derive_doc_type(ext_guess) or "word"
    cap = settings.max_upload_size_for(type_guess)

    # 2) 流式接收：分块落临时文件 + 计数 + 截取文件头
    tmp_path, size, head = await _spool_upload(file, cap, filename)
    try:
        # 3) 前置校验（写存储之前）：格式 / 危险扩展名 / 空文件 / 大小 / 魔数 / 类型提示
        ext, doc_type = validate_upload(filename, size, client_type, head)

        # 4) 可选病毒扫描（默认关闭；开启后 fail-closed）
        await virus_scan.scan_file(tmp_path)

        # 5) 生成相对 key 并写对象存储（流式上传，不整段读入内存）
        key = storage_mod.build_object_key(_parse_user_id(user_id), ext)
        # MIME 以扩展名映射为准：浏览器给 .md / .m4a 的 Content-Type 常不可靠
        mime_type = media_types.guess_mime(ext, file.content_type)
        try:
            await storage.put_object_from_path(key, tmp_path, mime_type)
        except storage_mod.StorageError:
            # 存储失败：DB 尚未写入，直接上抛（router 转 5xx），保证无脏记录
            raise

        # 6) 落库：写入文档元数据（原始文件不进库，仅存路径与元数据）
        doc = Document(
            user_id=_parse_user_id(user_id),
            filename=filename or f"untitled.{ext}",
            original_path=key,  # 相对 key（不含 bucket），便于换存储后端
            file_type=doc_type,
            mime_type=mime_type,
            size_bytes=size,
            status="pending",  # 提交即「待审核」，等待 P3 管理员审核
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
    finally:
        # 7) 临时文件使命完成：无论成功失败都清理（避免 /tmp 被大文件堆满）
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as exc:  # 清理失败只告警，不影响已完成的业务结果
            logger.warning("清理上传临时文件失败 path=%s：%s", tmp_path, exc)

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
