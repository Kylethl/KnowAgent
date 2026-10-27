# 对话历史与删除接口测试（P2-B1）
#
# 原则：用 AsyncMock 替代真实数据库，monkeypatch 服务层函数，验证「接口契约」
#       （参数解析、按 user_id 隔离、归属校验 404、软删除 204、响应结构）。
#       不依赖 PostgreSQL、不产生费用、不依赖网络。

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.conversations as conv_module
from app.core.security import DEMO_USER_ID
from app.main import create_app


def _fake_conv(id, title=None):
    return type(
        "Conv",
        (),
        {
            "id": id,
            "title": title,
            "model": "deepseek-flash",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    )()


def _fake_msg(id, role, content):
    return type(
        "Msg",
        (),
        {
            "id": id,
            "role": role,
            "content": content,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    )()


@pytest.fixture()
def client():
    """测试应用：覆盖鉴权依赖（返回固定用户）与数据库依赖（AsyncMock）。"""
    app = create_app()
    app.dependency_overrides[conv_module.get_current_user_id] = lambda: DEMO_USER_ID

    db = AsyncMock()
    # 列表接口的「消息计数」查询：await db.execute(...) 得到 Result，再调 Result.all()。
    # 注意：Result 必须用 MagicMock 承载——SQLAlchemy 2.x 里协程的是 execute()，
    # 而 Result.all() 是同步方法。若这里沿用 AsyncMock，就会把「await res.all()」
    # 这类真实运行会抛 TypeError 的错误写法也 mock 通过，从而掩盖线上 500。
    result = MagicMock()
    result.all.return_value = []  # 无消息 → message_count=0
    db.execute.return_value = result
    app.dependency_overrides[conv_module.get_db] = lambda: db
    return TestClient(app)


# ---------- 列表 ----------
def test_list_conversations_returns_summaries(client, monkeypatch):
    """GET /conversations 应返回会话摘要列表（含 message_count）。"""
    c1 = _fake_conv(uuid4(), title="会话A")
    monkeypatch.setattr(
        conv_module.svc, "list_conversations", AsyncMock(return_value=[c1])
    )
    resp = client.get("/conversations")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["id"] == str(c1.id)
    assert body[0]["title"] == "会话A"
    assert "message_count" in body[0]


# ---------- 新建 ----------
def test_create_conversation_returns_201(client, monkeypatch):
    """POST /conversations 应 201 并返回含 id 的会话。"""
    new_id = uuid4()
    monkeypatch.setattr(
        conv_module.svc,
        "get_or_create_conversation",
        AsyncMock(return_value=_fake_conv(new_id, title=None)),
    )
    resp = client.post("/conversations", json={})
    assert resp.status_code == 201
    assert resp.json()["id"] == str(new_id)


def test_create_conversation_with_title(client, monkeypatch):
    """带 title 新建时，服务层应被调用并能写回标题。"""
    captured = {}
    new_id = uuid4()

    async def fake_get(db, user_id, conversation_id=None):
        conv = _fake_conv(new_id)
        captured["conv"] = conv
        return conv

    monkeypatch.setattr(conv_module.svc, "get_or_create_conversation", fake_get)
    # 让 db.commit 成为可 await 的空操作
    db = AsyncMock()
    client.app.dependency_overrides[conv_module.get_db] = lambda: db

    resp = client.post("/conversations", json={"title": "我的标题"})
    assert resp.status_code == 201
    # 标题应被写回到返回体
    assert resp.json()["title"] == "我的标题"


# ---------- 历史（详情） ----------
def test_get_conversation_detail(client, monkeypatch):
    """GET /conversations/{id} 应返回会话与历史消息列表。"""
    cid = uuid4()
    conv = _fake_conv(cid, title="会话X")
    m1 = _fake_msg(uuid4(), "user", "你好")
    m2 = _fake_msg(uuid4(), "assistant", "你好呀")

    async def fake_detail(db, user_id, conversation_id):
        return conv, [m1, m2]

    monkeypatch.setattr(conv_module.svc, "get_conversation_detail", fake_detail)
    resp = client.get(f"/conversations/{cid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(cid)
    assert body["title"] == "会话X"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_get_conversation_not_found(client, monkeypatch):
    """归属校验失败（ConversationNotFound）应返回 404。"""
    async def fake_detail(db, user_id, conversation_id):
        from app.core.errors import ConversationNotFound
        raise ConversationNotFound(conversation_id)

    monkeypatch.setattr(conv_module.svc, "get_conversation_detail", fake_detail)
    resp = client.get(f"/conversations/{uuid4()}")
    assert resp.status_code == 404


def test_get_conversation_invalid_id(client):
    """会话 ID 格式非法也应返回 404（统一语义）。"""
    resp = client.get("/conversations/not-a-uuid")
    assert resp.status_code == 404


# ---------- 软删除 ----------
def test_delete_conversation_returns_204(client, monkeypatch):
    """DELETE /conversations/{id} 成功应返回 204（无响应体）。"""
    async def fake_delete(db, user_id, conversation_id):
        return None

    monkeypatch.setattr(conv_module.svc, "soft_delete_conversation", fake_delete)
    resp = client.delete(f"/conversations/{uuid4()}")
    assert resp.status_code == 204


def test_delete_conversation_not_found(client, monkeypatch):
    """删除不存在/越权的会话应返回 404。"""
    async def fake_delete(db, user_id, conversation_id):
        from app.core.errors import ConversationNotFound
        raise ConversationNotFound(conversation_id)

    monkeypatch.setattr(conv_module.svc, "soft_delete_conversation", fake_delete)
    resp = client.delete(f"/conversations/{uuid4()}")
    assert resp.status_code == 404
