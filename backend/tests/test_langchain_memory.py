# P1-A1 会话记忆单元测试（LangChain 版）
#
# 覆盖：消息类型转换、窗口截断、会话隔离、LRU 淘汰、客户端历史注入的一次性语义。
# 全部为纯内存逻辑，不依赖网络与大模型。

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.services.langchain_memory import (
    DEFAULT_CONVERSATION_ID,
    ConversationHistoryStore,
    WindowedChatMessageHistory,
    dicts_to_messages,
    get_memory_store,
    to_langchain_message,
)


# ---------- 类型转换 ----------
def test_to_langchain_message_maps_roles():
    """三种合法角色应映射为对应的 LangChain 消息类型。"""
    assert isinstance(to_langchain_message("user", "问"), HumanMessage)
    assert isinstance(to_langchain_message("assistant", "答"), AIMessage)
    assert isinstance(to_langchain_message("system", "设定"), SystemMessage)


def test_to_langchain_message_rejects_invalid():
    """空正文与非法角色应返回 None，避免脏数据透传给上游导致 400。"""
    assert to_langchain_message("user", "") is None
    assert to_langchain_message("tool", "xxx") is None


def test_dicts_to_messages_skips_invalid_items():
    """批量转换时应自动跳过非法项。"""
    messages = dicts_to_messages(
        [
            {"role": "user", "content": "你好"},
            {"role": "unknown", "content": "忽略我"},
            {"role": "assistant", "content": "嗨"},
        ]
    )
    assert [type(m).__name__ for m in messages] == ["HumanMessage", "AIMessage"]


# ---------- 窗口截断 ----------
def test_window_history_keeps_latest_turns():
    """超出窗口时应丢弃最旧的消息，按最近的 N 轮保留。"""
    history = WindowedChatMessageHistory("c1", max_turns=1)  # 即最多 2 条
    for i in range(3):
        history.add_messages(
            [HumanMessage(content=f"问{i}"), AIMessage(content=f"答{i}")]
        )

    contents = [m.content for m in history.messages]
    # 只保留最后一轮：问2 / 答2
    assert contents == ["问2", "答2"]


def test_window_history_drops_leading_assistant():
    """裁剪后若首条是助手消息应再丢弃，避免上下文以助手发言开头。"""
    history = WindowedChatMessageHistory("c1", max_turns=1)
    # 人为构造「助手在前」的历史
    history.add_messages([AIMessage(content="旧回答")])
    history.add_messages([HumanMessage(content="问题"), AIMessage(content="回答")])

    contents = [m.content for m in history.messages]
    assert contents == ["问题", "回答"]


def test_window_history_clear():
    """clear 应清空本会话全部历史。"""
    history = WindowedChatMessageHistory("c1")
    history.add_messages([HumanMessage(content="hi")])
    history.clear()
    assert history.messages == []


@pytest.mark.asyncio
async def test_async_variants_align_with_sync():
    """异步变体应与同步实现行为一致（LangChain 异步链路会调用异步变体）。"""
    history = WindowedChatMessageHistory("c1", max_turns=1)
    await history.aadd_messages([HumanMessage(content="问1"), AIMessage(content="答1")])
    await history.aadd_messages([HumanMessage(content="问2"), AIMessage(content="答2")])

    messages = await history.aget_messages()
    assert [m.content for m in messages] == ["问2", "答2"]

    await history.aclear()
    assert await history.aget_messages() == []


# ---------- 会话仓库 ----------
def test_store_returns_same_instance_per_session():
    """同一会话 ID 多次取用应返回同一实例，保证历史可持续累积。"""
    store = ConversationHistoryStore()
    first = store.get_session_history("conv-a")
    first.add_messages([HumanMessage(content="你好")])

    assert store.get_session_history("conv-a").messages[0].content == "你好"
    # 不同会话相互隔离
    assert store.get_session_history("conv-b").messages == []


def test_store_falls_back_to_default_session():
    """未传或空白会话 ID 应落到默认会话。"""
    store = ConversationHistoryStore()
    assert store.get_session_history(None) is store.get_session_history(None)
    assert store.get_session_history(None) is store.get_session_history("   ")
    assert store.get_session_history(DEFAULT_CONVERSATION_ID).messages == []


def test_store_evicts_least_recently_used():
    """会话数超过上限时应淘汰最久未使用的会话。"""
    store = ConversationHistoryStore(max_sessions=2)
    store.get_session_history("s1")
    store.get_session_history("s2")
    store.get_session_history("s1")  # 刷新 s1 的使用顺序
    store.get_session_history("s3")  # 触发淘汰，最久未使用的是 s2

    assert store.size() == 2
    assert store.peek_message_count("s2") == 0  # 已被淘汰（不存在该会话）


def test_store_peek_does_not_create_session():
    """peek 只做查看，不应创建空会话。"""
    store = ConversationHistoryStore()
    assert store.peek_message_count("nobody") == 0
    assert store.size() == 0


@pytest.mark.asyncio
async def test_seed_history_only_once():
    """客户端历史只应在会话为空时注入一次，否则每轮请求都会重复叠加。"""
    store = ConversationHistoryStore()
    items = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}]

    await store.seed_history("conv-seed", items)
    await store.seed_history("conv-seed", items)  # 第二次应被忽略

    assert len(store.get_session_history("conv-seed").messages) == 2


@pytest.mark.asyncio
async def test_clear_history():
    """清空后该会话应回到空状态，但会话对象仍可继续使用。"""
    store = ConversationHistoryStore()
    await store.seed_history("conv-clear", [{"role": "user", "content": "问"}])
    await store.clear_history("conv-clear")

    assert store.get_session_history("conv-clear").messages == []


def test_get_memory_store_is_singleton():
    """get_memory_store 应为进程内单例（多次调用返回同一对象）。"""
    get_memory_store.cache_clear()
    try:
        assert get_memory_store() is get_memory_store()
    finally:
        get_memory_store.cache_clear()
