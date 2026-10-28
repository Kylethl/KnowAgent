# P2 会话持久化路径测试（chat 接口集成）
#
# 验证 GET/POST /chat 在「数据库可用」时：
#   1) 解析/新建会话并发回 {type:conversation,id} 回绑帧；
#   2) 从数据库加载历史并作为唯一历史来源注入（memory_enabled=False，避免与内存双写）；
#   3) 流式结束后落库本轮对话（append_turn）。
#
# 方式：用 AsyncMock 替代 SessionLocal 会话、monkeypatch 服务层函数与 stream_answer，
#       不依赖真实 PostgreSQL / 大模型 / 网络。

from unittest.mock import AsyncMock
from uuid import UUID

import json
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_module
from app.core.security import DEMO_USER_ID, get_current_user_id_sse
from app.main import create_app


def _find_conversation_frame_id(frames):
    """从 SSE 帧列表中解析会话回绑帧，返回其 id（无则返回 None）。

    说明：SSE 内容帧形如 data: {"type": "conversation", "id": "..."}，
          json.dumps 默认在冒号后加空格，故按 JSON 解析而非字符串包含判断。
    """
    for f in frames:
        if not f.startswith("data: "):
            continue
        try:
            obj = json.loads(f[len("data: "):])
        except (ValueError, TypeError):
            continue
        if obj.get("type") == "conversation":
            return obj.get("id")
    return None


class _AsyncSessionCtx:
    """让 monkeypatch 后的 SessionLocal() 支持 `async with` 并返回假会话。"""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


@pytest.fixture()
def client():
    """创建测试应用并覆盖鉴权依赖（P2-B2 后 /chat 要求有效令牌）。

    用依赖覆盖注入固定 Demo 用户，让本文件专注验证「会话持久化链路」。
    """
    app = create_app()
    app.dependency_overrides[get_current_user_id_sse] = lambda: DEMO_USER_ID
    return TestClient(app)


@pytest.fixture(autouse=True)
def stub_retrieval(monkeypatch):
    """P4：隔离真实知识库检索（本文件只验证会话持久化，不关心检索）。

    返回 disabled 使 /chat 走「无检索」分支，避免真实调用 embedding 接口。
    """
    from app.services.retriever import MODE_DISABLED, RetrievalResult

    async def _fake_retrieval(query, db):
        return RetrievalResult(mode=MODE_DISABLED, reason="测试环境未启用检索")

    monkeypatch.setattr(chat_module, "retrieve_for_chat", _fake_retrieval)


def _install_db_path(monkeypatch, conv_id: str, history, captured: dict):
    """装配「数据库可用」路径所需的全部桩。"""
    db = AsyncMock()
    monkeypatch.setattr(chat_module, "SessionLocal", lambda: _AsyncSessionCtx(db))
    # P2-B2：当前用户已由路由的鉴权依赖（get_current_user_id_sse）解析，
    #        测试中通过 dependency_overrides 注入固定用户，无需再桩 resolve_user_id。

    async def fake_get_or_create(dbsession, user_id, conversation_id=None):
        conv = AsyncMock()
        conv.id = UUID(conv_id)
        return conv

    async def fake_load_history(dbsession, conversation_id, max_turns, token_budget):
        captured["history_requested"] = True
        return history

    async def fake_append(dbsession, conversation_id, user_content, assistant_content, citations=None):
        captured["appended"] = (user_content, assistant_content)
        # P4-B2：记录本轮引用来源，供「引用是否随消息落库」的断言使用
        captured["citations_saved"] = citations

    monkeypatch.setattr(chat_module.svc, "get_or_create_conversation", fake_get_or_create)
    monkeypatch.setattr(chat_module.svc, "load_history_window", fake_load_history)
    monkeypatch.setattr(chat_module.svc, "append_turn", fake_append)

    # 可控假流：记录入参并产出固定分片
    async def fake_stream(message, conversation_id=None, history=None, meta=None, memory_enabled=True, context=None):
        captured["memory_enabled"] = memory_enabled
        captured["history_passed"] = history
        if isinstance(meta, dict):
            meta["finish_reason"] = "stop"
        for piece in ["A", "B"]:
            yield piece

    monkeypatch.setattr(chat_module, "stream_answer", fake_stream)
    return db


def test_chat_emits_conversation_frame_and_injects_history(client, monkeypatch):
    """数据库路径：应下发会话回绑帧、用 DB 历史注入（memory_enabled=False）、结束落库。"""
    captured: dict = {}
    conv_id = "11111111-1111-1111-1111-111111111111"
    given_id = "22222222-2222-2222-2222-222222222222"
    _install_db_path(monkeypatch, conv_id, [{"role": "user", "content": "历史问题"}], captured)

    with client.stream(
        "GET", "/chat", params={"message": "你好", "conversation_id": given_id}
    ) as resp:
        frames = [line for line in resp.iter_lines() if line]

    # 1) 会话回绑帧存在且为新建/解析到的会话 ID
    assert _find_conversation_frame_id(frames) == conv_id, "缺少会话回绑帧"
    # 2) 流以 [DONE] 正常收尾
    assert frames[-1] == "data: [DONE]"
    # 3) 历史由数据库注入，且关闭了内存记忆（避免双写）
    assert captured.get("history_passed") == [{"role": "user", "content": "历史问题"}]
    assert captured.get("memory_enabled") is False
    # 4) 本轮对话被落库（用户提问 + 助手完整回答）
    assert captured.get("appended") == ("你好", "AB")


def test_chat_creates_conversation_when_no_id(client, monkeypatch):
    """未传 conversation_id 时应自动新建会话，并回绑新会话 ID。"""
    captured: dict = {}
    new_id = "33333333-3333-3333-3333-333333333333"
    given_history: list = []
    _install_db_path(monkeypatch, new_id, given_history, captured)

    with client.stream("GET", "/chat", params={"message": "首次提问"}) as resp:
        frames = [line for line in resp.iter_lines() if line]

    assert _find_conversation_frame_id(frames) == new_id
    # 无历史时应注入空列表
    assert captured.get("history_passed") == []
    assert captured.get("appended") == ("首次提问", "AB")
