# 会话服务层单元测试（P2-B1 / P2-DB1 / P2-A1）
#
# 原则：用 AsyncMock 替代真实数据库会话，聚焦「业务逻辑」而非 SQL 执行，
#       不依赖 PostgreSQL、不产生费用、不依赖网络。
#
# 覆盖：会话 CRUD（新建/列表/详情/软删除 + 归属校验）、长对话上下文窗口管理
#       （轮次窗口 + 令牌预算截断，防 token 溢出）、消息落库（标题回填/时间刷新）。

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import app.services.conversation_service as svc
from app.core.errors import ConversationNotFound
from app.db.models import Conversation, Message


def _mk_result(scalar=None, rows=None):
    """构造一次 db.execute 的返回结果对象（MagicMock）。

    - scalar_one_or_none() 返回 scalar（用于「归属校验」）；
    - scalars().all() / all() 返回 rows（用于「列表 / 详情 / 历史窗口」）。
    """
    res = MagicMock()
    res.scalar_one_or_none.return_value = scalar
    res.scalars.return_value.all.return_value = rows if rows is not None else []
    res.all.return_value = rows if rows is not None else []
    return res


def _db_with_results(*pairs):
    """构造 AsyncMock 数据库会话，按调用顺序返回预设结果。

    Args:
        pairs: 每个元素为 (scalar, rows)，对应一次 execute 的返回。
               若只有一组，则所有 execute 都返回它；多组则按调用顺序依次返回。

    Returns:
        配置好的 AsyncMock 会话对象。
    """
    db = AsyncMock()
    results = [_mk_result(s, r) for (s, r) in pairs]
    if len(results) == 1:
        db.execute.return_value = results[0]
    else:
        db.execute.side_effect = results
    return db


def _conv(id=None, title=None, deleted_at=None):
    """构造一个会话 ORM 对象（仅用于断言，不写库）。"""
    return Conversation(
        id=id or uuid4(),
        user_id=uuid4(),
        title=title,
        model="deepseek-flash",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1,1, tzinfo=timezone.utc),
        deleted_at=deleted_at,
    )


def _msg(role, content):
    """构造一条消息 ORM 对象。"""
    return Message(
        id=uuid4(),
        conversation_id=uuid4(),
        role=role,
        content=content,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ---------- 新建会话 ----------
@pytest.mark.asyncio
async def test_get_or_create_creates_when_none():
    """conversation_id 为 None 时应新建会话（add + flush），并返回同一对象。"""
    db = _db_with_results()  # 新建路径不查询
    user_id = uuid4()
    conv = await svc.get_or_create_conversation(db, user_id, None)
    assert isinstance(conv, Conversation)
    assert conv.user_id == user_id
    db.add.assert_called_once()
    db.flush.assert_awaited_once()


# ---------- 归属校验：不存在 → 404 ----------
@pytest.mark.asyncio
async def test_get_or_create_not_found_raises():
    """指定会话不存在时抛出 ConversationNotFound（统一 404 语义）。"""
    user_id = uuid4()
    target = uuid4()
    # 查询返回空（scalar_one_or_none = None）
    db = _db_with_results((None, None))
    with pytest.raises(ConversationNotFound):
        await svc.get_or_create_conversation(db, user_id, target)


# ---------- 列表 ----------
@pytest.mark.asyncio
async def test_list_conversations_returns_inputs():
    """列表应把 SQL 过滤后的行原样返回（deleted_at IS NULL 过滤由 SQL 完成）。"""
    kept = _conv(title="A")
    deleted = _conv(title="B", deleted_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    # 列表只做 scalars().all()，返回预设行
    db = _db_with_results((None, [kept, deleted]))
    result = await svc.list_conversations(db, uuid4())
    assert result == [kept, deleted]


# ---------- 详情 ----------
@pytest.mark.asyncio
async def test_get_conversation_detail_orders_messages():
    """详情应返回会话与按时间正序的消息列表。"""
    conv = _conv(title="会话X")
    m1 = _msg("user", "你好")
    m2 = _msg("assistant", "你好呀")
    # 第一次 execute：归属校验（scalar_one_or_none）返回 conv；
    # 第二次 execute：消息查询（scalars().all）返回 [m1, m2]
    db = _db_with_results((conv, None), (None, [m1, m2]))
    got_conv, got_msgs = await svc.get_conversation_detail(db, uuid4(), conv.id)
    assert got_conv is conv
    assert [m.role for m in got_msgs] == ["user", "assistant"]


# ---------- 软删除 ----------
@pytest.mark.asyncio
async def test_soft_delete_marks_deleted_at():
    """软删除应打上 deleted_at 时间戳并提交，不物理删除。"""
    conv = _conv(title="待删")
    db = _db_with_results((conv, None))
    await svc.soft_delete_conversation(db, uuid4(), conv.id)
    assert conv.deleted_at is not None
    db.commit.assert_awaited_once()


# ---------- 上下文窗口管理（P2-DB1） ----------
@pytest.mark.asyncio
async def test_load_history_window_keeps_recent_turns():
    """轮次窗口：超过 max_turns*2 条时只保留最近部分。"""
    # 构造 6 条消息（3 轮），max_turns=2 → 应保留最近 4 条
    many = []
    for i in range(3):
        many += [_msg("user", f"q{i}"), _msg("assistant", f"a{i}")]
    db = _db_with_results((None, many))
    window = await svc.load_history_window(db, uuid4(), max_turns=2, token_budget=0)
    assert len(window) == 4
    # 最近 4 条应为第 3 轮 + 第 2 轮
    assert window[-1]["content"] == "a2"


@pytest.mark.asyncio
async def test_load_history_window_token_budget_truncates():
    """令牌预算：从最旧往新丢弃，直到不超预算（至少保留 1 条）。"""
    msgs = [_msg("user", "短问题"), _msg("assistant", "短回答")]
    db = _db_with_results((None, msgs))
    window = await svc.load_history_window(db, uuid4(), max_turns=100, token_budget=1)
    assert len(window) == 1
    assert window[0]["content"] == "短回答"


@pytest.mark.asyncio
async def test_load_history_window_returns_role_content_pairs():
    """返回结构应为 {role, content} 列表，可直接喂给 LangChain。"""
    msgs = [_msg("user", "问"), _msg("assistant", "答")]
    db = _db_with_results((None, msgs))
    window = await svc.load_history_window(db, uuid4(), max_turns=10, token_budget=0)
    assert window == [
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "答"},
    ]


# ---------- 消息落库（标题回填 + 时间刷新） ----------
@pytest.mark.asyncio
async def test_append_turn_backfills_title_and_bumps_updated_at():
    """首轮：用首条用户问题回填标题，并刷新 updated_at；两条消息落库。"""
    conv = _conv(title=None)
    db = _db_with_results((conv, None))

    before = conv.updated_at
    await svc.append_turn(db, conv.id, "这是第一个问题", "这是回答")
    # 两条消息被 add
    assert db.add.call_count == 2
    # 标题回填
    assert conv.title == "这是第一个问题"
    # 更新时间被刷新（必然晚于创建时间）
    assert conv.updated_at > before
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_append_turn_keeps_existing_title():
    """非首轮（已有标题）时不应覆盖原有标题。"""
    conv = _conv(title="已存在标题")
    db = _db_with_results((conv, None))

    await svc.append_turn(db, conv.id, "新问题", "新回答")
    assert conv.title == "已存在标题"
