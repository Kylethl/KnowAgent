# 会话与消息的数据访问 / 业务逻辑（P2-B1 / P2-DB1 / P2-A1）
# ---------------------------------------------------------------------
# 职责：把「按 user_id 隔离的会话 CRUD」「历史回看」「软删除」
#       「长对话上下文窗口管理（截断，防 token 溢出）」收敛到一处，
#       供 api/conversations.py 与 api/chat.py 复用，避免 SQL 散落在路由层。
#
# 设计要点：
#   - 所有查询都带 user_id 过滤，天然实现《需求清单》P2-DB1 的「会话/消息按 user_id 隔离」。
#   - 软删除只用 deleted_at 标记，不真删：列表/历史查询统一用 deleted_at IS NULL 过滤，
#     满足 DELETE /conversations/{id} 的「软删除」约定；消息随会话级联保留，便于误删恢复。
#   - 上下文窗口管理（P2-DB1）：load_history_window 只回传「最近若干轮 + 令牌预算内」的历史，
#     超出部分丢弃，防止长对话把整段历史塞进 prompt 导致 token 溢出（截断策略；
#     摘要策略在 P2 后续可叠加，此处以截断为默认、成本最低的防溢出手段）。

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ConversationNotFound
from ..core.logging import get_logger
from ..db.models import Conversation, Message

logger = get_logger(__name__)

# 单条消息 token 估算比例：中文约 1 token ≈ 1.5~2 字，这里用 len//4 做保守估算，
# 仅用于上下文窗口预算判断，不追求精确（精确分词在 P4 检索阶段才需要）。
_CHARS_PER_TOKEN = 4

# 会话标题最大长度：首条用户问题回填标题时截断，避免超 VARCHAR(255)
_TITLE_MAX_LEN = 50


def estimate_tokens(text: str) -> int:
    """按字符数粗略估算 token 数（无第三方分词依赖，足够用于窗口预算判断）。

    Args:
        text: 待估算的文本。

    Returns:
        估算的 token 数（至少 1，空文本记 1 以便预算计算）。
    """
    return max(1, len(text) // _CHARS_PER_TOKEN)


async def get_or_create_conversation(
    db: AsyncSession,
    user_id: UUID,
    conversation_id: Optional[UUID] = None,
) -> Conversation:
    """获取指定会话（校验归属），为空则新建一个会话。

    关键校验：
        - 指定 conversation_id 时，必须同时满足「存在 / 属于该用户 / 未软删除」，
          否则抛出 ConversationNotFound（统一 404 语义，不泄露「是否存在/是否他人」）。

    Args:
        db: 数据库会话。
        user_id: 当前用户 ID（隔离与归属校验用）。
        conversation_id: 指定的会话 ID；为 None 时新建。

    Returns:
        会话 ORM 对象（新建时已 flush，可直接拿到自增 id）。

    Raises:
        ConversationNotFound: 会话不存在 / 已删除 / 不属于当前用户。
    """
    if conversation_id is None:
        # 新建会话：title 先留空，首条用户消息到达时由 chat 链路回填（见 append_turn）。
        conv = Conversation(user_id=user_id)
        db.add(conv)
        # flush 立即拿到生成的 UUID，便于后续写消息与返回前端
        await db.flush()
        logger.info("新建会话：id=%s，user_id=%s", conv.id, user_id)
        return conv

    # 查询时同时约束归属与未删除，避免越权读取他人会话
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    conv = result.scalar_one_or_none()
    if conv is None:
        # 统一 404 语义：不区分「不存在」与「不属于你」，避免泄露信息
        raise ConversationNotFound(conversation_id)
    return conv


async def list_conversations(db: AsyncSession, user_id: UUID) -> List[Conversation]:
    """列出当前用户的会话（排除已软删除），按更新时间倒序。

    说明：本接口只返回会话元数据（不含消息），适合左侧会话列表渲染。

    Args:
        db: 数据库会话。
        user_id: 当前用户 ID。

    Returns:
        会话 ORM 对象列表（updated_at 倒序）。
    """
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id, Conversation.deleted_at.is_(None))
        .order_by(Conversation.updated_at.desc())
    )
    return list(result.scalars().all())


async def get_conversation_detail(
    db: AsyncSession, user_id: UUID, conversation_id: UUID
) -> tuple[Conversation, List[Message]]:
    """获取会话详情及其全部消息（按时间正序），用于历史回看。

    Args:
        db: 数据库会话。
        user_id: 当前用户 ID。
        conversation_id: 会话 ID（需归属校验）。

    Returns:
        (会话对象, 消息对象列表)。消息按 created_at 正序，便于前端按时间渲染。

    Raises:
        ConversationNotFound: 会话不存在 / 已删除 / 不属于当前用户。
    """
    conv = await get_or_create_conversation(db, user_id, conversation_id)
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    messages = list(result.scalars().all())
    return conv, messages


async def soft_delete_conversation(
    db: AsyncSession, user_id: UUID, conversation_id: UUID
) -> None:
    """软删除会话：仅标记 deleted_at，不物理删除，消息随会话级联保留。

    Args:
        db: 数据库会话。
        user_id: 当前用户 ID。
        conversation_id: 会话 ID（需归属校验）。

    Raises:
        ConversationNotFound: 会话不存在 / 已删除 / 不属于当前用户。
    """
    conv = await get_or_create_conversation(db, user_id, conversation_id)
    conv.deleted_at = datetime.now(timezone.utc)  # 打上软删除时间戳
    # 软删除后 updated_at 也刷新，使列表排序/缓存失效更直观
    conv.updated_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("软删除会话：id=%s，user_id=%s", conversation_id, user_id)


async def append_turn(
    db: AsyncSession,
    conversation_id: UUID,
    user_content: str,
    assistant_content: str,
    citations: Optional[List[Dict]] = None,
) -> tuple:
    """把一轮「用户提问 + 助手回答」写入消息表，并更新会话元数据。

    副作用：
      - 新增两条消息（role=user / role=assistant），各记 token_count（窗口管理用）；
      - P4-B2：助手消息额外写入 citations（引用来源），历史回看时据此还原引用卡片；
      - 若该会话尚无标题，用首条用户问题前若干字回填 title；
      - 刷新 conversation.updated_at，使左侧列表按「最近活跃」置顶。

    Args:
        db: 数据库会话（调用方负责提交/回滚边界；本函数统一在末尾 commit）。
        conversation_id: 会话 ID。
        user_content: 本轮用户提问文本。
        assistant_content: 本轮助手完整回答文本。
        citations: 本轮回答的引用来源列表（doc_id + 片段等），无引用时传 None。

    Returns:
        (user_message_id, assistant_message_id) 二元组。

    P6 为什么需要返回 ID：
        反馈接口 /messages/{id}/feedback 需要一个「真实的消息主键」。
        前端在流式过程中用的是自己生成的消息 ID（src/hooks/useChatStream.ts），
        与数据库主键无关；因此后端落库后必须把生成的 ID 回传给前端，
        否则点赞/点踩按钮没有可以指向的目标。
    """
    # 1) 写入两条消息（token_count 用于后续上下文窗口预算）
    user_message = Message(
        conversation_id=conversation_id,
        role="user",
        content=user_content,
        token_count=estimate_tokens(user_content),
    )
    assistant_message = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=assistant_content,
        token_count=estimate_tokens(assistant_content),
        # 引用关系随消息一起落库（JSONB 列）：这是《需求清单》P4 功能2
        # 「引用关系存 messages 附加字段」的落地方式。
        # 存 None 而非空列表：便于区分「当时未启用检索」与「检索了但无引用」。
        citations=citations or None,
    )
    db.add(user_message)
    db.add(assistant_message)

    # 1.1) flush 拿到数据库生成的主键（UUID 由 PG 的 gen_random_uuid() 产生，
    #      不 flush 就取不到，也就无法回传给前端做反馈）
    await db.flush()

    # 2) 取回会话对象，做标题回填与时间刷新
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if conv is not None:
        if not conv.title and user_content:
            # 首轮：用首条用户问题生成标题（截断到 _TITLE_MAX_LEN 字）
            conv.title = user_content[:_TITLE_MAX_LEN]
        # 刷新更新时间，让最近活跃的会话排在列表前面
        conv.updated_at = datetime.now(timezone.utc)

    # 3) 一次性提交（消息 + 会话元数据）
    await db.commit()
    logger.info(
        "已落库一轮对话：conversation_id=%s，用户 %d 字 / 助手 %d 字",
        conversation_id,
        len(user_content),
        len(assistant_content),
    )

    # 4) 回传两条消息的主键：调用方（/chat）据此下发 message 帧，供前端绑定反馈按钮
    return user_message.id, assistant_message.id


async def load_history_window(
    db: AsyncSession,
    conversation_id: UUID,
    max_turns: int,
    token_budget: int,
) -> List[Dict[str, str]]:
    """加载注入 LangChain 的历史窗口（P2-DB1 上下文窗口管理）。

    两步裁剪（与 P1 内存记忆的窗口逻辑对齐，但作用在持久化消息上）：
      1. 轮次窗口：保留最近 max_turns*2 条（1 轮 = 1 用户 + 1 助手）；
      2. 令牌预算：在窗口内从最旧往新累加 token，超出预算则丢弃最旧部分
         （至少保留最后一条，保证本轮必有上下文），防 prompt token 溢出。

    Args:
        db: 数据库会话。
        conversation_id: 会话 ID。
        max_turns: 保留的历史轮次上限。
        token_budget: 历史令牌预算上限（0/负表示不限制令牌）。

    Returns:
        形如 [{"role": "user"/"assistant", "content": "..."}] 的历史列表，可直接喂给 LangChain。
    """
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    msgs = list(result.scalars().all())
    original_count = len(msgs)

    # 1) 轮次窗口：丢弃最旧的多余消息
    limit = max(max_turns, 0) * 2
    if limit and len(msgs) > limit:
        dropped = len(msgs) - limit
        msgs = msgs[-limit:]
        logger.info(
            "会话 %s 历史超轮次窗口：丢弃最旧 %d 条，保留 %d 条",
            conversation_id,
            dropped,
            limit,
        )

    # 2) 令牌预算：从最旧往新丢弃，直到不超预算（至少保留 1 条）
    if token_budget and token_budget > 0:
        total = sum(estimate_tokens(m.content) for m in msgs)
        while total > token_budget and len(msgs) > 1:
            removed = msgs.pop(0)
            total -= estimate_tokens(removed.content)
            logger.debug("会话 %s 超令牌预算，丢弃最旧一条历史", conversation_id)

    if original_count != len(msgs):
        logger.info(
            "会话 %s 上下文窗口裁剪：%d → %d 条（防 token 溢出）",
            conversation_id,
            original_count,
            len(msgs),
        )

    # 转成 LangChain 友好的字典列表（role/content）
    return [{"role": m.role, "content": m.content} for m in msgs]
