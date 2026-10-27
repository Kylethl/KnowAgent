# 会话上下文记忆（P1-A1：LangChain 版）
# 职责：为调用链提供「按会话隔离」的多轮历史存储，替代 P1-B1 的进程内临时实现。
#
# 设计要点：
#   1. **遵循 LangChain 标准接口**：继承 langchain_core 的 BaseChatMessageHistory，
#      这是 LangChain 记忆体系的契约基类；P2 换 Redis / PostgreSQL 实现时只需替换本文件的存储后端，
#      上层调用链与 API 层零改动。
#   2. **窗口截断**：只保留最近 MEMORY_MAX_TURNS 轮对话，天然防 token 溢出
#      （P2 会再叠加「长对话摘要」做更精细的上下文窗口管理）。
#   3. **会话上限（LRU）**：限制单进程内同时保留的会话数量，避免长期运行内存无限增长。
#   4. **并发安全**：用 threading.Lock 保护共享结构。锁内只有内存操作、没有 IO 或 await，
#      因此同步/异步两条路径都能安全使用（P2 落库后由数据库事务保证一致性）。
#
# ⚠️ 说明：与 P1-B1 一样，本实现仍是**进程内内存**，服务重启即丢失、多副本不共享；
#       这是 P1「单会话内多轮」的正确取舍——持久化留到 P2（P2-DB1 / P2-A1 落 PostgreSQL messages 表）。

from __future__ import annotations

import threading
from collections import OrderedDict
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from ..config import get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)

# 前端未传 conversation_id 时使用的默认会话键（P2 接入真实会话后由前端传入）
DEFAULT_CONVERSATION_ID = "default"

# 一条外部历史消息的结构：{"role": "user"|"assistant"|"system", "content": "..."}
HistoryItem = Dict[str, str]


def to_langchain_message(role: str, content: str) -> Optional[BaseMessage]:
    """把 {"role","content"} 形式的字典转换为 LangChain 消息对象。

    Args:
        role: 消息角色；非法角色返回 None，由调用方丢弃（避免脏数据导致上游 400）。
        content: 消息正文。

    Returns:
        对应的 LangChain 消息对象；非法输入返回 None。
    """
    if not content:
        return None
    if role == "user":
        return HumanMessage(content=content)
    if role == "assistant":
        return AIMessage(content=content)
    if role == "system":
        return SystemMessage(content=content)
    # 其余角色（如 tool / function / 未知值）在 P1 对话场景无意义，直接丢弃
    return None


def dicts_to_messages(items: Iterable[HistoryItem]) -> List[BaseMessage]:
    """批量把历史字典转换为 LangChain 消息列表（自动跳过非法项）。"""
    messages: List[BaseMessage] = []
    for item in items or []:
        message = to_langchain_message(item.get("role", ""), item.get("content", ""))
        if message is not None:
            messages.append(message)
    return messages


class WindowedChatMessageHistory(BaseChatMessageHistory):
    """单会话的历史记录：在 LangChain 契约之上加了「窗口截断」能力。

    之所以自己实现而不是直接用 InMemoryChatMessageHistory：
        P1 需要「限制上下文长度以防 token 溢出」这一行为，且 langchain-core 1.x
        已将内置的内存实现标记为 deprecated，自实现反而更可控、也便于 P2 平滑替换为持久化实现。
    """

    def __init__(self, conversation_id: str, max_turns: int = 10):
        """
        Args:
            conversation_id: 会话标识。
            max_turns: 保留的历史轮次上限；1 轮 = 1 条用户消息 + 1 条助手消息。
        """
        self._conversation_id = conversation_id
        self._max_turns = max_turns
        self._messages: List[BaseMessage] = []
        # 多个会话共享同一个 store 锁；此处为本会话的消息列表单独加锁，避免并发写入错乱
        self._lock = threading.Lock()

    # ---------- LangChain 契约要求的成员 ----------
    @property
    def conversation_id(self) -> str:
        """会话标识（便于日志与调试）。"""
        return self._conversation_id

    @property
    def messages(self) -> List[BaseMessage]:
        """读取历史消息副本（外部修改不会影响内部状态）。"""
        with self._lock:
            return list(self._messages)

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        """追加消息并按窗口截断。

        Args:
            messages: 要追加的消息序列（通常为本轮的 [HumanMessage, AIMessage]）。
        """
        if not messages:
            return
        with self._lock:
            self._messages.extend(messages)
            self._trim_locked()

    def clear(self) -> None:
        """清空本会话历史（前端「新建会话」时使用）。"""
        with self._lock:
            self._messages = []

    # ---------- 异步变体：直接复用同步实现（P1 无 IO，无需真正的异步） ----------
    async def aget_messages(self) -> List[BaseMessage]:
        """异步读取历史（LangChain 在异步链路上会优先调用此变体）。"""
        return self.messages

    async def aadd_messages(self, messages: Sequence[BaseMessage]) -> None:
        """异步追加消息。"""
        self.add_messages(messages)

    async def aclear(self) -> None:
        """异步清空历史。"""
        self.clear()

    # ---------- 内部实现 ----------
    def _trim_locked(self) -> None:
        """按窗口裁剪历史（调用前必须已持锁）。

        两步处理：
          1. 超出「轮次上限 × 2」条时保留最近的部分，防止上下文无限膨胀；
          2. 若裁剪后首条是助手消息，再丢弃它——避免上下文以助手发言开头
             （部分模型对「必须以 user 起手或 user/assistant 交替」敏感）。
        """
        limit = max(self._max_turns, 0) * 2
        if limit and len(self._messages) > limit:
            dropped = len(self._messages) - limit
            self._messages = self._messages[-limit:]
            logger.debug(
                "会话 %s 历史超出窗口：丢弃最旧的 %d 条，保留 %d 条",
                self._conversation_id,
                dropped,
                limit,
            )
        if self._messages and isinstance(self._messages[0], AIMessage):
            self._messages = self._messages[1:]


class ConversationHistoryStore:
    """会话历史仓库：管理多个 WindowedChatMessageHistory 实例。

    职责边界：只做「会话 → 历史对象」的映射与容量治理，
    不参与任何提示词或模型逻辑。
    """

    def __init__(self, max_turns: int = 10, max_sessions: int = 1000):
        """
        Args:
            max_turns: 单会话保留的轮次上限。
            max_sessions: 进程内同时保留的会话数量上限，超出后按 LRU 淘汰最久未使用的会话。
        """
        self._max_turns = max_turns
        self._max_sessions = max_sessions
        # OrderedDict 实现简易 LRU：get 时把会话移到末尾，淘汰时弹出队首
        self._sessions: "OrderedDict[str, WindowedChatMessageHistory]" = OrderedDict()
        self._lock = threading.Lock()

    def get_session_history(self, conversation_id: Optional[str]) -> WindowedChatMessageHistory:
        """获取（或惰性创建）指定会话的历史对象。

        Args:
            conversation_id: 会话 ID；为空或纯空白则使用默认会话。

        Returns:
            该会话对应的历史对象（同一 ID 多次调用返回同一实例，故状态可持续累积）。
        """
        key = (conversation_id or "").strip() or DEFAULT_CONVERSATION_ID
        with self._lock:
            history = self._sessions.get(key)
            if history is None:
                history = WindowedChatMessageHistory(key, max_turns=self._max_turns)
                self._sessions[key] = history
                self._evict_locked()
                logger.info("创建会话记忆：conversation_id=%s", key)
            else:
                # 命中：更新使用顺序，避免被 LRU 误淘汰
                self._sessions.move_to_end(key)
            return history

    async def seed_history(self, conversation_id: Optional[str], items: Iterable[HistoryItem]) -> None:
        """把客户端显式携带的历史写入服务端记忆。

        为什么只在会话为空时写入：
            POST /chat 允许客户端回传完整历史（P2 会话历史场景），
            若不判空会在每轮请求后重复叠加同一批历史，导致上下文指数膨胀。

        Args:
            conversation_id: 会话 ID。
            items: 历史消息字典列表。
        """
        messages = dicts_to_messages(items)
        if not messages:
            return
        history = self.get_session_history(conversation_id)
        if len(history.messages) == 0:
            history.add_messages(messages)
            logger.info(
                "已注入客户端历史 %d 条：conversation_id=%s",
                len(messages),
                history.conversation_id,
            )
        else:
            logger.debug(
                "会话 %s 已存在历史，跳过客户端历史注入（避免重复叠加）",
                history.conversation_id,
            )

    async def clear_history(self, conversation_id: Optional[str]) -> None:
        """清空指定会话的历史。"""
        key = (conversation_id or "").strip() or DEFAULT_CONVERSATION_ID
        with self._lock:
            history = self._sessions.get(key)
            if history is not None:
                history.clear()
                logger.info("已清空会话记忆：conversation_id=%s", key)

    def peek_message_count(self, conversation_id: Optional[str]) -> int:
        """只读地查看某会话已有的历史条数（不会创建空会话）。"""
        key = (conversation_id or "").strip() or DEFAULT_CONVERSATION_ID
        with self._lock:
            history = self._sessions.get(key)
            return 0 if history is None else len(history.messages)

    def size(self) -> int:
        """当前进程内维护的会话数量（便于 /health 观测与调试）。"""
        with self._lock:
            return len(self._sessions)

    def _evict_locked(self) -> None:
        """超过会话上限时淘汰最久未使用的会话（调用前必须已持锁）。"""
        while len(self._sessions) > self._max_sessions:
            evicted_key, _ = self._sessions.popitem(last=False)
            logger.warning("会话数超过上限 %d，淘汰最久未使用会话：%s", self._max_sessions, evicted_key)


@lru_cache(maxsize=1)
def get_memory_store() -> ConversationHistoryStore:
    """获取全局唯一的会话历史仓库（进程内单例）。

    单测中若需要干净的记忆状态，先调用 get_memory_store.cache_clear()。
    """
    settings = get_settings()
    logger.info(
        "初始化 LangChain 会话记忆：max_turns=%d，max_sessions=%d",
        settings.MEMORY_MAX_TURNS,
        settings.MEMORY_MAX_SESSIONS,
    )
    return ConversationHistoryStore(
        max_turns=settings.MEMORY_MAX_TURNS,
        max_sessions=settings.MEMORY_MAX_SESSIONS,
    )


def get_session_history(conversation_id: Optional[str]) -> WindowedChatMessageHistory:
    """会话历史工厂：供调用链按 session_id 取历史对象（LangChain 生态的标准取法）。"""
    return get_memory_store().get_session_history(conversation_id)
