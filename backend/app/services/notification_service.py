# 站内信 / 审核通知服务（P6-B1）
# ---------------------------------------------------------------------
# 职责：把「审核结果通知」这件事从路由层与审核状态机里剥离出来，集中管理
#       「通知生成」「通知查询」「未读统计」「标记已读」四块逻辑。
#
# 契约来源（《需求清单》P6 功能1「审核通知」）：
#   - 前端：站内信 / Toast 提示「文档已通过 / 被驳回」
#   - 后端：状态变更触发通知（通过/驳回后发消息）
#   - 数据存储：notifications 表
#
# 关键设计（为什么这么写）：
#   1. **同事务写入**：create_review_notification 只 db.add 不 commit，
#      由调用方（review_service.review_document）在同一个事务里连同审核结果一起提交。
#      这样不会出现「审核成功但通知丢失」的静默不一致——
#      「审核结论」与「通知收件人看到的内容」必须同生共死。
#   2. **正文存快照**：title / content 在生成时固化，不依赖 documents 表实时拼装。
#      文档改名、被下线、甚至被清理后，历史通知仍然是当时的事实描述。
#   3. **无收件人则不发**：P1 阶段允许匿名提交（documents.user_id 为空），
#      这类文档没有可通知的对象，直接跳过而不是写入一条 user_id 为空的脏数据。
#
# 未读数统计策略：单独提供 count_unread，配合部分索引
# （idx_notifications_user_unread，见 sql/007）只扫描未读行，轮询成本极低。

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import NotificationNotFound
from ..core.logging import get_logger
from ..db.models import Document, Notification

logger = get_logger(__name__)

# 通知类型常量：与 notifications.type 的 CHECK 约束、前端 NotificationItem.type 严格一致
TYPE_DOCUMENT_APPROVED = "document_approved"
TYPE_DOCUMENT_REJECTED = "document_rejected"

# 列表分页默认值与硬上限：与审核队列（review_service）保持同一套约定，
# 避免前端在不同列表间遇到不同的分页语义
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# 通知正文中引用的驳回理由最大长度：理由本身可能很长（管理员自由填写），
# 截断到该长度避免一条通知把前端抽屉撑爆；完整理由仍可在「我的文档」中查看
NOTE_MAX_LEN = 200


def normalize_pagination(limit: Optional[int], offset: Optional[int]) -> Tuple[int, int]:
    """规范化分页参数：把非法/超界值收敛到安全区间。

    Args:
        limit: 每页条数；None 或非法时用默认值，超过 MAX_PAGE_SIZE 时截断。
        offset: 偏移量；None 或负数时归零。

    Returns:
        (limit, offset) 规范化后的二元组。
    """
    try:
        real_limit = int(limit) if limit is not None else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        real_limit = DEFAULT_PAGE_SIZE
    # 下限 1、上限 MAX_PAGE_SIZE（通知列表比审核队列更轻，上限取更小的 50）
    real_limit = max(1, min(real_limit, MAX_PAGE_SIZE))

    try:
        real_offset = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        real_offset = 0
    # 负偏移归零，避免 SQL 抛错
    real_offset = max(0, real_offset)

    return real_limit, real_offset


def build_review_message(doc: Document, action: str) -> Tuple[str, str]:
    """根据审核结论生成通知的标题与正文（纯函数，便于单测）。

    Args:
        doc: 已审核的文档（提供 filename 与 review_note）。
        action: 审核动作，'approved' 或 'rejected'。

    Returns:
        (title, content) 二元组：一句话结论 + 含细节的正文。

    Raises:
        ValueError: action 不是 approved / rejected（调用方应先做状态机校验）。
    """
    filename = doc.filename or "未命名文档"

    if action == "approved":
        title = f"文档《{filename}》已通过审核"
        # 通过时说明「接下来会发生什么」：用户最关心的是「能不能用上了」。
        # 用「入库」而非「写入知识库」，与前端状态标签（入库中 / 已入库）保持同一套术语。
        content = (
            f"你提交的文档《{filename}》已通过管理员审核，"
            "系统正在解析并入库，完成后即可在问答中被检索引用。"
        )
        # 管理员可填写通过备注（可选），有则附上，便于用户了解审核意见
        note = (doc.review_note or "").strip()
        if note:
            content += f"\n审核备注：{note[:NOTE_MAX_LEN]}"
        return title, content

    if action == "rejected":
        title = f"文档《{filename}》被驳回"
        # 驳回必须带理由（review_service 已强制校验），这里做一次截断兜底
        reason = (doc.review_note or "").strip()[:NOTE_MAX_LEN] or "未填写具体理由"
        content = (
            f"你提交的文档《{filename}》未通过审核。\n"
            f"驳回理由：{reason}\n"
            "可按理由修改后重新提交。"
        )
        return title, content

    # 状态机之外的动作：属于调用方 bug，快速失败而不是发一条语义不明的通知
    raise ValueError(f"未知的审核动作：{action}")


async def create_review_notification(
    db: AsyncSession,
    doc: Document,
    action: str,
) -> Optional[Notification]:
    """为一次审核结果生成通知（仅 add 到会话，**不 commit**）。

    事务约定（重要）：
        本函数只把通知加入当前会话的待提交集合，由调用方与审核字段一起 commit。
        这样「审核结论」与「通知」要么同时生效、要么同时回滚，
        不会出现「文档已通过但用户没收到通知」的静默丢失。

    Args:
        db: 数据库会话（与审核动作同一个会话）。
        doc: 已写入审核结论的文档对象。
        action: 审核动作，'approved' 或 'rejected'。

    Returns:
        新建的 Notification 对象；文档无提交人（匿名提交）时返回 None。

    Raises:
        ValueError: action 非法（build_review_message 抛出）。
    """
    # 1) 无收件人则不发：P1 允许匿名提交，这类文档没有人可通知
    if doc.user_id is None:
        logger.info("文档 %s 无提交人，跳过审核通知", doc.id)
        return None

    # 2) 生成标题与正文（内部已校验 action 合法性）
    notification_type = (
        TYPE_DOCUMENT_APPROVED if action == "approved" else TYPE_DOCUMENT_REJECTED
    )
    title, content = build_review_message(doc, action)

    # 3) 落库（不提交）：user_id 为收件人，doc_id 供前端定位文档
    notification = Notification(
        user_id=doc.user_id,
        type=notification_type,
        title=title,
        content=content,
        doc_id=doc.id,
    )
    db.add(notification)

    logger.info(
        "已生成审核通知：doc=%s，收件人=%s，类型=%s",
        doc.id, doc.user_id, notification_type,
    )
    return notification


async def list_notifications(
    db: AsyncSession,
    user_id: UUID,
    unread_only: bool = False,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Tuple[List[Notification], int, int]:
    """查询当前用户的站内信（按创建时间倒序）。

    Args:
        db: 异步数据库会话。
        user_id: 收件人（当前登录用户）。
        unread_only: True 时只返回未读通知（前端「只看未读」筛选）。
        limit: 每页条数。
        offset: 偏移量。

    Returns:
        (items, total, unread_count) 三元组：
          items       当前页通知列表；
          total       满足筛选条件的总数（分页用）；
          unread_count 未读总数（**不受 unread_only 影响**，保证角标始终准确）。
    """
    real_limit, real_offset = normalize_pagination(limit, offset)

    # 基础过滤：只看自己的通知（user_id 由 JWT 解析，不接受客户端传入）
    conditions = [Notification.user_id == user_id]
    if unread_only:
        conditions.append(Notification.read_at.is_(None))

    # 总数：与列表共用同一套筛选条件，保证分页准确
    total = int(
        (await db.execute(select(func.count()).select_from(Notification).where(*conditions))).scalar()
        or 0
    )

    # 列表：最新在前（用户最关心刚发生的事）
    rows = (
        (
            await db.execute(
                select(Notification)
                .where(*conditions)
                .order_by(Notification.created_at.desc())
                .limit(real_limit)
                .offset(real_offset)
            )
        )
        .scalars()
        .all()
    )

    # 未读数单独统计：即使前端筛了「只看未读」，角标也要反映真实未读总量
    unread = await count_unread(db, user_id)
    return list(rows), total, unread


async def count_unread(db: AsyncSession, user_id: UUID) -> int:
    """统计当前用户的未读通知数（顶栏铃铛角标的数据源）。

    Args:
        db: 异步数据库会话。
        user_id: 当前登录用户。

    Returns:
        未读条数（无未读时为 0）。
    """
    stmt = (
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return int((await db.execute(stmt)).scalar() or 0)


async def mark_read(db: AsyncSession, user_id: UUID, notification_id) -> Notification:
    """把一条通知标记为已读（幂等：已读再标记不改时间）。

    Args:
        db: 异步数据库会话。
        user_id: 当前登录用户（同时作为归属校验条件）。
        notification_id: 通知 ID（字符串或 UUID）。

    Returns:
        更新后的 Notification 对象。

    Raises:
        NotificationNotFound: 通知不存在 / 不属于当前用户 / ID 格式非法。
    """
    # 1) ID 解析：非法格式与「找不到」同处理，避免暴露库内 ID 规则
    try:
        nid = notification_id if isinstance(notification_id, UUID) else UUID(str(notification_id))
    except (ValueError, AttributeError, TypeError):
        raise NotificationNotFound(notification_id)

    # 2) 取通知（带 user_id 条件：别人的通知一律视为不存在）
    stmt = select(Notification).where(
        Notification.id == nid,
        Notification.user_id == user_id,
    )
    notification = (await db.execute(stmt)).scalar_one_or_none()
    if notification is None:
        raise NotificationNotFound(notification_id)

    # 3) 已读则保持原时间（幂等），未读则写入当前时间
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(notification)
        logger.debug("通知已标记已读：id=%s，user=%s", nid, user_id)

    return notification


async def mark_all_read(db: AsyncSession, user_id: UUID) -> int:
    """把当前用户的全部未读通知标记为已读（一键清空角标）。

    用单条 UPDATE 而非「先查再逐条改」：未读可能有上百条，
    逐条 flush 既慢又占内存，批量更新一条 SQL 即可。

    Args:
        db: 异步数据库会话。
        user_id: 当前登录用户。

    Returns:
        本次实际标记的条数（0 表示本来就没有未读）。
    """
    stmt = (
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    result = await db.execute(stmt)
    await db.commit()

    # rowcount 为受影响行数；部分驱动可能返回 None，做个兜底
    updated = int(getattr(result, "rowcount", 0) or 0)
    if updated:
        logger.info("用户 %s 一键已读：%d 条通知", user_id, updated)
    return updated


def to_payload(notification: Notification) -> dict:
    """把 ORM 通知对象转换为接口响应字典（ISO 时间、字符串 ID）。

    抽成函数而非在路由里逐字段拼装：列表、标记已读两处都要用同一套映射，
    集中一处可避免「一个接口返回的是 UUID、另一个是 str」这类不一致。

    Args:
        notification: Notification ORM 对象。

    Returns:
        可直接喂给 NotificationOut 的字典。
    """
    return {
        "id": str(notification.id),
        "type": notification.type,
        "title": notification.title,
        "content": notification.content,
        "doc_id": str(notification.doc_id) if notification.doc_id else None,
        "read_at": notification.read_at.isoformat() if notification.read_at else None,
        "created_at": notification.created_at.isoformat() if notification.created_at else "",
    }
