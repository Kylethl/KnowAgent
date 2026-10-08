# 会话上下文记忆（P1-B1 临时实现）
# 职责：在单会话内保存最近若干轮对话，供 /chat 注入到 DeepSeek 请求，实现多轮连续问答。
#
# ⚠️ 定位说明（避免与后续任务重复造轮子）：
#   - 本实现为**进程内字典**，服务重启即丢失，且多副本部署时不共享，仅满足 P1 单实例演示；
#   - P1-A1 将用 LangChain memory 接管多轮记忆的组装逻辑；
#   - P2（P2-DB1 / P2-A1）会改为从 PostgreSQL 的 messages 表加载历史并做上下文窗口管理。
#   因此本模块保持极小体积，接口设计为「append / history / clear」三件事，便于整体替换。

import asyncio
from collections import deque
from functools import lru_cache
from typing import Deque, Dict, List, Optional

from ..config import get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)

# 前端未传 conversation_id 时使用的默认会话键（P2 接入真实会话后由前端传入）
DEFAULT_CONVERSATION_ID = "default"

# 一条记忆消息的结构：{"role": "user"|"assistant", "content": "..."}
MemoryItem = Dict[str, str]


class InMemoryConversationMemory:
    """基于内存的会话记忆存储。

    每个会话维护一个固定容量的双端队列（deque），超出容量自动丢弃最旧的轮次，
    从而天然限制上下文长度、避免 token 溢出。
    """

    def __init__(self, max_turns: int = 10):
        """
        Args:
            max_turns: 保留的历史轮次上限；1 轮 = 1 条用户消息 + 1 条助手消息。
        """
        self._max_turns = max_turns
        # 容量按「消息条数」计：一轮两条，故 ×2
        self._store: Dict[str, Deque[MemoryItem]] = {}
        # 并发写保护：避免同一会话的两轮请求交错写入导致顺序错乱
        self._lock = asyncio.Lock()

    def _bucket(self, conversation_id: Optional[str]) -> Deque[MemoryItem]:
        """获取（或惰性创建）某会话的消息队列。"""
        key = conversation_id or DEFAULT_CONVERSATION_ID
        if key not in self._store:
            self._store[key] = deque(maxlen=self._max_turns * 2)
        return self._store[key]

    async def append(self, conversation_id: Optional[str], role: str, content: str) -> None:
        """向会话追加一条消息。

        Args:
            conversation_id: 会话 ID；为空则写入默认会话。
            role: 消息角色（user / assistant）。
            content: 消息正文；空内容直接忽略，避免污染上下文。
        """
        if not content:
            return
        async with self._lock:
            self._bucket(conversation_id).append({"role": role, "content": content})

    async def history(self, conversation_id: Optional[str]) -> List[MemoryItem]:
        """读取会话的历史消息（按时间正序，已按容量截断）。

        Args:
            conversation_id: 会话 ID；为空则读默认会话。

        Returns:
            历史消息列表的副本（外部修改不会影响内部存储）。
        """
        async with self._lock:
            return list(self._bucket(conversation_id))

    async def clear(self, conversation_id: Optional[str]) -> None:
        """清空指定会话的记忆（前端「新建会话」时使用）。"""
        async with self._lock:
            self._store.pop(conversation_id or DEFAULT_CONVERSATION_ID, None)

    def size(self) -> int:
        """当前进程内维护的会话数量（便于 /health 与调试观察）。"""
        return len(self._store)


@lru_cache(maxsize=1)
def get_memory() -> InMemoryConversationMemory:
    """获取全局唯一的会话记忆实例（进程内单例）。"""
    settings = get_settings()
    logger.info("初始化进程内会话记忆：max_turns=%d", settings.MEMORY_MAX_TURNS)
    return InMemoryConversationMemory(max_turns=settings.MEMORY_MAX_TURNS)
