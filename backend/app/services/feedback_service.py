# 答案反馈服务（P6-B2）
# ---------------------------------------------------------------------
# 职责：把「对回答点赞/点踩」这件事从路由层剥离，集中管理
#       「提交/修改反馈」「查询反馈」「取消反馈」「会话历史批量回显」「质量评估统计」。
#
# 契约来源（《需求清单》P6 功能2「答案反馈与评估」）：
#   - 前端：回答点赞/点踩按钮 + 反馈表单
#   - 后端：/messages/{id}/feedback 存储反馈
#   - AI Agent：反馈采集用于后续 prompt/检索离线优化
#   - 数据存储：feedback 表
#
# 关键设计：
#   1. **评价对象是「一条助手回答」**：因此必须校验 message.role == 'assistant'，
#      对用户自己的提问打分毫无意义，还会污染统计口径。
#   2. **归属校验走 conversation.user_id**：知识库共享，但对话是私有的。
#      任何人都不能给别人的回答打差评（也会被用来探测他人对话是否存在）。
#   3. **重复提交 = 修改**（upsert）：用户「先赞后踩」很常见，
#      应覆盖原记录而不是堆两条互相矛盾的反馈（DB 侧有唯一索引兜底）。
#   4. **取消反馈 = 物理删除**：反馈不像会话那样需要留痕，
#      用户点错了要能撤销；统计的是「当前态度」，不是历史操作日志。

from __future__ import annotations

from typing import Dict, Iterable, List, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import FeedbackNotAllowed, MessageNotFound
from ..core.logging import get_logger
from ..db.models import Conversation, Feedback, Message

logger = get_logger(__name__)

# 合法评价结论；与 feedback.rating 的 CHECK 约束、前端按钮一一对应
RATING_UP = "up"
RATING_DOWN = "down"
RATINGS = (RATING_UP, RATING_DOWN)

# 合法点踩归因；与前端的表单选项、CHECK 约束三处一致
REASONS = ("off_topic", "wrong", "unsupported", "other")

# 备注长度上限：与 schemas.FEEDBACK_COMMENT_MAX_LEN 保持一致
# （schema 层先挡一次，这里再挡一次——服务层可能被后台脚本/任务直接调用）
COMMENT_MAX_LEN = 1000

# 管理员评估视图中「回答摘要」的截断长度：只看开头就够判断答得好不好
ANSWER_SNIPPET_LEN = 300

# 管理员评估视图中「用户提问」的截断长度
QUESTION_SNIPPET_LEN = 200


def _parse_uuid(value) -> Optional[UUID]:
    """把任意输入解析为 UUID；非法输入返回 None（由调用方统一转成 404 语义）。

    Args:
        value: 字符串 / UUID / 其它任意值。

    Returns:
        UUID 对象；无法解析时为 None。
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def _load_answer_message(
    db: AsyncSession, user_id: UUID, message_id
) -> Message:
    """取出「属于当前用户的助手回答」消息，否则抛错。

    一次查询完成三件事：存在性、归属（会话属于当前用户）、角色（必须是助手）。
    为什么合并：反馈提交是高频轻操作，能少一次往返就少一次。

    Args:
        db: 异步数据库会话。
        user_id: 当前登录用户。
        message_id: 消息 ID（字符串或 UUID）。

    Returns:
        Message 对象（role='assistant'，且所属会话属于该用户）。

    Raises:
        MessageNotFound: ID 非法 / 消息不存在 / 会话不属于当前用户 / 消息已被清理。
        FeedbackNotAllowed: 消息存在且属于该用户，但不是助手回答。
    """
    mid = _parse_uuid(message_id)
    if mid is None:
        # ID 格式非法：与「找不到」同处理，避免暴露库内 ID 规则
        raise MessageNotFound(message_id)

    # 内连接 conversations 做归属校验：只有本人的会话才可能出现在结果里
    stmt = (
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == mid,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),  # 已删除会话里的回答不可再评价
        )
    )
    message = (await db.execute(stmt)).scalar_one_or_none()
    if message is None:
        raise MessageNotFound(message_id)

    # 只能评价助手回答：对用户提问/系统消息打分没有业务含义
    if message.role != "assistant":
        raise FeedbackNotAllowed("只能对助手回答进行评价")
    return message


async def submit_feedback(
    db: AsyncSession,
    user_id: UUID,
    message_id,
    rating: str,
    reason: Optional[str] = None,
    comment: Optional[str] = None,
) -> Feedback:
    """提交或修改一条反馈（同一用户对同一条回答只有一条记录）。

    入参规范化规则：
      - rating 必须为 up / down；
      - rating=up 时忽略 reason（点赞没有归因语义，避免统计被污染）；
      - reason 必须在合法集合内（非空时）；
      - comment 去首尾空白，空串视为未填写，超长直接拒绝。

    Args:
        db: 异步数据库会话。
        user_id: 当前登录用户（评价人，来自 JWT）。
        message_id: 被评价的回答消息 ID。
        rating: 评价结论 up / down。
        reason: 点踩归因（可选）。
        comment: 补充说明（可选，≤ COMMENT_MAX_LEN）。

    Returns:
        新建或更新后的 Feedback 对象（已 commit + refresh）。

    Raises:
        MessageNotFound: 消息不存在 / 不属于当前用户。
        FeedbackNotAllowed: 不是助手回答 / rating 非法 / 归因非法 / 备注超长。
    """
    # 1) 评价结论合法性（schema 已限制，这里防御服务层被直接调用的情况）
    if rating not in RATINGS:
        raise FeedbackNotAllowed(f"非法的评价结论：{rating}")

    # 2) 归属与角色校验（同时确认消息存在）
    await _load_answer_message(db, user_id, message_id)
    mid = _parse_uuid(message_id)  # 上面已校验过，这里必定成功

    # 3) 归因规范化：点赞不留归因；点踩的归因必须在白名单内
    clean_reason = (reason or "").strip() or None
    if rating == RATING_UP:
        clean_reason = None
    elif clean_reason is not None and clean_reason not in REASONS:
        raise FeedbackNotAllowed(f"非法的反馈原因：{clean_reason}")

    # 4) 备注规范化：去空白、空串转 None、超长拒绝
    clean_comment = (comment or "").strip() or None
    if clean_comment is not None and len(clean_comment) > COMMENT_MAX_LEN:
        raise FeedbackNotAllowed(f"补充说明最多 {COMMENT_MAX_LEN} 字")

    # 5) upsert：先查后写（唯一索引是最终兜底，这里避免常规路径上的冲突异常）
    existing = (
        await db.execute(
            select(Feedback).where(
                Feedback.message_id == mid,
                Feedback.user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        feedback = Feedback(
            message_id=mid,
            user_id=user_id,
            rating=rating,
            reason=clean_reason,
            comment=clean_comment,
        )
        db.add(feedback)
        action = "新增"
    else:
        # 覆盖式修改：以用户「最新的态度」为准
        existing.rating = rating
        existing.reason = clean_reason
        existing.comment = clean_comment
        feedback = existing
        action = "更新"

    await db.commit()
    await db.refresh(feedback)

    logger.info(
        "反馈%s：message=%s，user=%s，rating=%s，reason=%s",
        action, mid, user_id, rating, clean_reason,
    )
    return feedback


async def get_feedback(
    db: AsyncSession, user_id: UUID, message_id
) -> Optional[Feedback]:
    """查询当前用户对某条回答的反馈（未评价返回 None）。

    说明：这里**不**校验消息是否存在 —— 历史消息可能已被清理，
    前端回显只需知道「有没有评价过」，查不到就当未评价，
    不必为此报 404 让整个历史页面报错。

    Args:
        db: 异步数据库会话。
        user_id: 当前登录用户。
        message_id: 消息 ID。

    Returns:
        Feedback 对象或 None。
    """
    mid = _parse_uuid(message_id)
    if mid is None:
        return None
    stmt = select(Feedback).where(
        Feedback.message_id == mid,
        Feedback.user_id == user_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def delete_feedback(db: AsyncSession, user_id: UUID, message_id) -> bool:
    """取消当前用户对某条回答的反馈（物理删除）。

    Args:
        db: 异步数据库会话。
        user_id: 当前登录用户。
        message_id: 消息 ID。

    Returns:
        True 表示确实删除了一条反馈；False 表示本来就没有（幂等）。
    """
    existing = await get_feedback(db, user_id, message_id)
    if existing is None:
        return False

    await db.delete(existing)
    await db.commit()
    logger.info("取消反馈：message=%s，user=%s", message_id, user_id)
    return True


async def load_feedback_map(
    db: AsyncSession, user_id: UUID, message_ids: Iterable[UUID]
) -> Dict[UUID, Feedback]:
    """批量取当前用户对一批消息的反馈（会话历史回显用，一次查询避免 N+1）。

    Args:
        db: 异步数据库会话。
        user_id: 当前登录用户。
        message_ids: 消息 ID 集合。

    Returns:
        {message_id: Feedback} 映射；未评价的消息不在其中。
    """
    ids = [mid for mid in message_ids if mid is not None]
    if not ids:
        return {}

    rows = (
        (
            await db.execute(
                select(Feedback).where(
                    Feedback.user_id == user_id,
                    Feedback.message_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return {f.message_id: f for f in rows}


def to_payload(feedback: Feedback) -> dict:
    """把 ORM 反馈对象转换为接口响应字典（ISO 时间、字符串 ID）。

    Args:
        feedback: Feedback ORM 对象。

    Returns:
        可直接喂给 FeedbackOut 的字典。
    """
    return {
        "id": str(feedback.id),
        "message_id": str(feedback.message_id),
        "rating": feedback.rating,
        "reason": feedback.reason,
        "comment": feedback.comment,
        "created_at": feedback.created_at.isoformat() if feedback.created_at else "",
        "updated_at": feedback.updated_at.isoformat() if feedback.updated_at else "",
    }


async def build_summary(db: AsyncSession, recent_limit: int = 10) -> dict:
    """汇总反馈数据，生成「答案质量评估」概览（管理员用）。

    包含三部分：
      1. 计数：总数 / 赞 / 踩 / 好评率；
      2. 归因分布：点踩原因各多少条（决定下一步优化方向的关键数据）；
      3. 最近差评明细：带「用户提问 + 回答摘要」的上下文，
         只看一个 message_id 是没法判断问题出在哪的。

    Args:
        db: 异步数据库会话。
        recent_limit: 最近差评返回条数（默认 10，上限 50）。

    Returns:
        可直接喂给 FeedbackSummaryResponse 的字典。
    """
    recent_limit = max(1, min(int(recent_limit or 10), 50))

    # 1) 计数：一次分组查询算出 up / down 的条数，避免两次全表扫描
    counts = dict(
        (
            await db.execute(
                select(Feedback.rating, func.count()).group_by(Feedback.rating)
            )
        ).all()
    )
    up_count = int(counts.get(RATING_UP, 0))
    down_count = int(counts.get(RATING_DOWN, 0))
    total = up_count + down_count
    # 好评率：无反馈时记 0（不做除零）
    up_rate = round(up_count / total, 4) if total else 0.0

    # 2) 归因分布：只统计点踩且填了归因的记录
    reason_rows = (
        await db.execute(
            select(Feedback.reason, func.count())
            .where(Feedback.rating == RATING_DOWN, Feedback.reason.is_not(None))
            .group_by(Feedback.reason)
        )
    ).all()
    reason_breakdown = {str(reason): int(cnt) for reason, cnt in reason_rows}

    # 3) 最近差评：先取反馈本身，再补上下文（问题 / 回答）
    recent = (
        (
            await db.execute(
                select(Feedback)
                .where(Feedback.rating == RATING_DOWN)
                .order_by(Feedback.created_at.desc())
                .limit(recent_limit)
            )
        )
        .scalars()
        .all()
    )
    recent_items = await _attach_context(db, list(recent))

    return {
        "total": total,
        "up_count": up_count,
        "down_count": down_count,
        "up_rate": up_rate,
        "reason_breakdown": reason_breakdown,
        "recent_down": recent_items,
    }


async def _attach_context(db: AsyncSession, feedbacks: List[Feedback]) -> List[dict]:
    """为一批差评补充「所属会话 / 用户提问 / 回答摘要」。

    实现说明（为什么不逐条查）：
        每条反馈都单独查消息与提问会产生 2N 次查询。这里改成两次批量查询：
          ① 取全部相关回答消息（含 conversation_id）；
          ② 取这些会话里的用户消息，按时间排序后在内存里配对。
        差评数量被 recent_limit 限制在 50 以内，内存开销可忽略。

    Args:
        db: 异步数据库会话。
        feedbacks: 差评记录列表（按时间倒序）。

    Returns:
        FeedbackDownItem 结构的字典列表。
    """
    if not feedbacks:
        return []

    # ① 回答消息：拿到 conversation_id 与正文
    msg_ids = [f.message_id for f in feedbacks]
    answers = (
        (
            await db.execute(
                select(Message).where(Message.id.in_(msg_ids))
            )
        )
        .scalars()
        .all()
    )
    answer_by_id = {m.id: m for m in answers}
    conv_ids = {m.conversation_id for m in answers}

    # ② 这些会话里的用户提问：按时间升序取出，之后为每条回答找「之前最近的一条」
    questions: Dict[UUID, List[Message]] = {}
    if conv_ids:
        rows = (
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id.in_(conv_ids),
                        Message.role == "user",
                    )
                    .order_by(Message.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            questions.setdefault(row.conversation_id, []).append(row)

    items: List[dict] = []
    for fb in feedbacks:
        answer = answer_by_id.get(fb.message_id)
        if answer is None:
            # 回答已被清理（会话被彻底删除）：仍返回反馈本身，只是没有上下文
            items.append(
                {
                    "feedback_id": str(fb.id),
                    "message_id": str(fb.message_id),
                    "conversation_id": "",
                    "question": "",
                    "answer": "",
                    "reason": fb.reason,
                    "comment": fb.comment,
                    "created_at": fb.created_at.isoformat() if fb.created_at else "",
                }
            )
            continue

        items.append(
            {
                "feedback_id": str(fb.id),
                "message_id": str(fb.message_id),
                "conversation_id": str(answer.conversation_id),
                "question": _pick_question(
                    questions.get(answer.conversation_id, []), answer
                )[:QUESTION_SNIPPET_LEN],
                "answer": (answer.content or "")[:ANSWER_SNIPPET_LEN],
                "reason": fb.reason,
                "comment": fb.comment,
                "created_at": fb.created_at.isoformat() if fb.created_at else "",
            }
        )
    return items


def _pick_question(candidates: List[Message], answer: Message) -> str:
    """在同会话的用户消息中，找出「该回答对应的那条提问」。

    规则：取时间不晚于回答、且最接近回答的那条用户消息（正常一问一答的配对结果）。
    时间戳缺失或并列时退化为「最后一条用户消息」—— 宁可配错一条，
    也好过在评估视图里显示空白（管理员无从判断问题是什么）。

    Args:
        candidates: 同会话的用户消息（按时间升序）。
        answer: 该条助手回答。

    Returns:
        提问正文；找不到时返回空串。
    """
    if not candidates:
        return ""

    answer_at = answer.created_at
    picked: Optional[Message] = None
    for msg in candidates:
        # created_at 理论上非空（DB NOT NULL），但历史数据/内存桩可能缺失，故做判空
        if answer_at is not None and msg.created_at is not None:
            if msg.created_at <= answer_at:
                picked = msg
            else:
                break
        else:
            picked = msg

    # 兜底：时间信息不可比时用最后一条用户消息
    return (picked or candidates[-1]).content or ""
