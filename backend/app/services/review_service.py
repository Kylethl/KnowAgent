# 文档审核服务（P3-B1：管理员审核）
# ---------------------------------------------------------------------
# 职责：把「管理员审核」这件事从路由层剥离，集中管理
#       「审核队列查询」「审核状态机」「文档内容预览」三块逻辑。
#
# 契约来源（《需求清单》P3 功能1「管理员审核」）：
#   - 前端：管理员后台页、待审核列表、文档预览、通过/驳回（驳回必填理由）
#   - 后端：/admin/documents 列表、/admin/documents/{id}/review 更新状态，RBAC 校验仅 admin
#   - 数据存储：documents 扩展 review_status、reviewer_id、review_note、reviewed_at
#
# 字段语义（与 sql/003_p3_db1_review.sql 的约定严格一致）：
#   status        整体生命周期：pending → approved/rejected → indexing → indexed
#   review_status 审核结论：pending / approved / rejected，**不随入库流程变化**
#   审核动作中两者同步写入；入库阶段只改 status，review_status 保持 approved。
#
# 状态机：
#   pending ──通过──▶ approved   （status 同步为 approved，等待 P3-B2 入库）
#   pending ──驳回──▶ rejected   （status 同步为 rejected，必须填写驳回理由）
#   rejected ──重审通过──▶ approved
#   approved ──▶ 终态，不允许重复审核或驳回（需下线走 P3-B2 的入库管理）

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import DocumentNotFound, ReviewNotAllowed
from ..core.logging import get_logger
from ..db.models import Document, User
from ..services import notification_service
from ..services import storage as storage_mod

logger = get_logger(__name__)

# 合法的审核动作；与前端 AdminPage 的按钮严格对应，不接受其它取值
REVIEW_APPROVE = "approved"
REVIEW_REJECT = "rejected"
REVIEW_ACTIONS = (REVIEW_APPROVE, REVIEW_REJECT)

# 预览文本的最大字符数：超出即截断（前端展示用，避免大文件把响应撑爆）
PREVIEW_MAX_CHARS = 5000

# 列表分页默认值与硬上限（防止一次性拉全表拖垮接口）
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def normalize_pagination(limit: Optional[int], offset: Optional[int]) -> Tuple[int, int]:
    """规范化分页参数：把非法/超界值收敛到安全区间。

    Args:
        limit: 每页条数；为 None 或非法时用默认值，超过 MAX_PAGE_SIZE 时截断。
        offset: 偏移量；为 None 或负数时归零。

    Returns:
        (limit, offset) 规范化后的二元组。
    """
    try:
        real_limit = int(limit) if limit is not None else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        real_limit = DEFAULT_PAGE_SIZE
    # 下限 1、上限 MAX_PAGE_SIZE
    real_limit = max(1, min(real_limit, MAX_PAGE_SIZE))

    try:
        real_offset = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        real_offset = 0
    # 负偏移归零，避免 SQL 抛错
    real_offset = max(0, real_offset)

    return real_limit, real_offset


async def list_review_queue(
    db: AsyncSession,
    review_status: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Tuple[List[Document], int]:
    """查询管理员审核队列（可按审核结论与文件名关键字筛选，按提交时间倒序）。

    Args:
        db: 异步数据库会话。
        review_status: 审核结论过滤（pending / approved / rejected）；None 表示不过滤。
        keyword: 文件名模糊匹配关键字（大小写不敏感）；None 表示不过滤。
        limit: 每页条数。
        offset: 偏移量。

    Returns:
        (documents, total) 二元组：当前页文档列表 + 满足条件的总数（供前端分页）。

    Raises:
        ReviewNotAllowed: review_status 取值不在合法集合内（快速失败，避免拼出无意义 SQL）。
    """
    if review_status is not None and review_status not in ("pending", "approved", "rejected"):
        raise ReviewNotAllowed(f"非法的审核状态筛选值：{review_status}")

    real_limit, real_offset = normalize_pagination(limit, offset)

    # 基础过滤：排除软删除文档（deleted_at 为空）
    conditions = [Document.deleted_at.is_(None)]
    if review_status:
        conditions.append(Document.review_status == review_status)
    if keyword and keyword.strip():
        # ilike 大小写不敏感；对 % 与 _ 做转义，避免用户输入的通配符影响匹配
        safe_kw = keyword.strip().replace("%", "\\%").replace("_", "\\_")
        conditions.append(Document.filename.ilike(f"%{safe_kw}%"))

    # 总数查询：与列表共用同一套过滤条件，保证分页总数准确
    total_stmt = select(func.count()).select_from(Document).where(*conditions)
    total = int((await db.execute(total_stmt)).scalar() or 0)

    # 列表查询：按提交时间倒序（管理员先看最新提交），分页取当前页
    list_stmt = (
        select(Document)
        .where(*conditions)
        .order_by(Document.created_at.desc())
        .limit(real_limit)
        .offset(real_offset)
    )
    rows = (await db.execute(list_stmt)).scalars().all()

    logger.debug(
        "审核队列查询：review_status=%s，keyword=%s，返回 %d/%d 条",
        review_status, keyword, len(rows), total,
    )
    return list(rows), total


async def get_document_for_review(db: AsyncSession, document_id) -> Document:
    """按 ID 取待审核文档（不存在或已软删除时抛错）。

    Args:
        db: 异步数据库会话。
        document_id: 文档 UUID（可传字符串，内部统一转换）。

    Returns:
        Document 对象。

    Raises:
        DocumentNotFound: 文档不存在 / 已被软删除 / ID 格式非法。
    """
    try:
        doc_uuid = document_id if isinstance(document_id, UUID) else UUID(str(document_id))
    except (ValueError, AttributeError, TypeError):
        # ID 格式非法：与「找不到」同处理，避免向调用方暴露库内细节
        raise DocumentNotFound(document_id)

    stmt = select(Document).where(
        Document.id == doc_uuid,
        Document.deleted_at.is_(None),
    )
    doc = (await db.execute(stmt)).scalar_one_or_none()
    if doc is None:
        raise DocumentNotFound(document_id)
    return doc


async def review_document(
    db: AsyncSession,
    doc: Document,
    reviewer: User,
    action: str,
    note: Optional[str] = None,
) -> Document:
    """执行一次审核动作（通过 / 驳回），并落定审核结论与生命周期状态。

    P6-B1 联动：审核结论落定的同时生成站内信（见 notification_service），
    与审核字段同一事务提交——提交人下次轮询未读数时即可看到结果。

    校验顺序（先便宜后昂贵，先安全后写入）：
      1. 动作取值必须合法；
      2. 已通过的文档是终态，不允许重复审核或驳回；
      3. 驳回必须填写理由（《需求清单》明确要求「驳回必填理由」）。

    Args:
        db: 异步数据库会话。
        doc: 待审核文档（已由 get_document_for_review 取到）。
        reviewer: 执行审核的管理员（由 require_admin 保证其 role='admin'）。
        action: 审核动作，'approved' 或 'rejected'。
        note: 审核备注；驳回时必填。

    Returns:
        更新后的 Document 对象（已 commit + refresh）。

    Raises:
        ReviewNotAllowed: 动作非法 / 状态冲突 / 驳回未填理由。
    """
    # 1) 动作合法性
    if action not in REVIEW_ACTIONS:
        raise ReviewNotAllowed(f"非法的审核动作：{action}")

    # 2) 状态机校验：approved 是终态，禁止再改（避免把已入库文档打回）
    if doc.review_status == REVIEW_APPROVE:
        raise ReviewNotAllowed("该文档已审核通过，不能重复审核或驳回")

    # 3) 驳回必填理由：需求硬性要求，保证提交人能知道被拒原因
    clean_note = (note or "").strip()
    if action == REVIEW_REJECT and not clean_note:
        raise ReviewNotAllowed("驳回时必须填写驳回理由")

    # 4) 写入审核结论：review_status 与 status 同步，保证「审核队列」与「入库管线」一致
    doc.review_status = action
    doc.status = action
    doc.reviewer_id = reviewer.id
    # 通过时备注可选（可留空）；驳回时上面已保证非空
    doc.review_note = clean_note or None
    doc.reviewed_at = datetime.now(timezone.utc)

    # 5) P6-B1：生成站内信（审核结果通知）。
    #    注意：这里只是 db.add，**不单独 commit** —— 通知与审核字段在同一事务提交，
    #    保证「审核结论」与「用户收到的通知」同生共死，不会出现
    #    「文档已通过但提交人始终收不到通知」的静默不一致。
    #    收件人为空（匿名提交）时服务内部会直接跳过并返回 None。
    try:
        await notification_service.create_review_notification(db, doc, action)
    except ValueError as exc:
        # 动作非法理论上已被上面拦截，这里仅防御性兜底：
        # 通知生成失败不应让审核动作白白回滚（用户仍能看到状态变化）
        logger.error("生成审核通知失败（不影响审核结果）：doc=%s，原因=%s", doc.id, exc)

    await db.commit()
    await db.refresh(doc)

    logger.info(
        "文档审核完成：id=%s，filename=%s，动作=%s，审核人=%s",
        doc.id, doc.filename, action, reviewer.username,
    )
    return doc


def _extract_text_from_bytes(content: bytes, file_type: str) -> str:
    """从文件字节中提取纯文本（仅供管理员预览，不落库、不参与入库）。

    - markdown（.md）：直接按 UTF-8 解码（容错替换非法字节）；
    - word（.docx）：用 python-docx 读取段落文本（P3-A1 解析策略的前置复用）；
    - word（.doc）：二进制旧格式，python-docx 不支持，返回明确提示而非报错。

    Args:
        content: 文件二进制内容。
        file_type: 文档类型（'word' / 'markdown'）。

    Returns:
        提取出的文本；无法解析时返回可读的提示文案。
    """
    if file_type == "markdown":
        # Markdown 本身就是纯文本，直接解码即可（errors='replace' 避免非法字节中断）
        return content.decode("utf-8", errors="replace")

    if file_type == "word":
        try:
            from docx import Document as DocxDocument  # 懒导入：仅在预览 Word 时需要

            docx = DocxDocument(BytesIO(content))
            # 段落文本按行拼接；空段落保留为空行，尽量还原原文结构
            paragraphs = [p.text for p in docx.paragraphs]
            return "\n".join(paragraphs)
        except Exception:  # noqa: BLE001 —— 预览失败不应中断审核流程
            # .doc（旧二进制格式）或损坏文件：给出明确提示，管理员可改用下载查看
            logger.warning("Word 文档预览解析失败（可能是 .doc 旧格式或文件损坏）")
            return "（该文件为旧版 .doc 二进制格式或已损坏，无法在线预览，请下载后查看）"

    # 其余类型（P5 的 image/audio/video）不通过字节解析：它们的文本由 OCR/ASR 产生，
    # 见 preview_document 中的媒体分支（直接读 documents.extracted_text，不再解原文件）
    return f"（{file_type} 类型需经 OCR/ASR 解析后查看文本，见「解析结果」）"


async def preview_document(
    storage: storage_mod.BaseStorage,
    doc: Document,
    max_chars: int = PREVIEW_MAX_CHARS,
) -> Tuple[str, bool]:
    """生成文档的预览内容（管理员审核时快速判断文档质量）。

    分两条路径：
      - 图片/音频/视频：不回读原始文件（视频可达 200MB），直接返回上一次 OCR/ASR
        的结果；尚未解析时给出可读提示，管理员仍可通过「播放/查看原文件」判断；
      - 文档：读取原始文件并按类型提取纯文本（原有逻辑）。

    Args:
        storage: 存储后端实例（用于读取原始文件字节）。
        doc: 文档对象（提供 original_path / file_type / extracted_text）。
        max_chars: 预览最大字符数，超出截断。

    Returns:
        (text, truncated) 二元组：text 为预览文本（已截断则 truncated=True）。

    Raises:
        storage_mod.StorageError: 原始文件读取失败（仅文档类会读文件）。
    """
    # 1) 多模态：优先展示已提取的文本（审核时最能说明「这条知识讲了什么」）
    if doc.file_type in ("image", "audio", "video"):
        extracted = (doc.extracted_text or "").strip()
        if extracted:
            return (extracted[:max_chars], len(extracted) > max_chars)
        return (
            "（该文件为图片/音视频，尚未完成 OCR/ASR 解析。"
            "审核通过并入库后，可在此查看解析出的文字/转写文本。）",
            False,
        )

    # 2) 文档：从对象存储读取原始字节后解析
    content = await storage.get_object(doc.original_path)

    # 3) 按类型提取纯文本
    text = _extract_text_from_bytes(content, doc.file_type)

    # 4) 截断保护：超长文本只返回前 max_chars 字符，并明确告知前端已截断
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False
