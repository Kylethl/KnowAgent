# P1-A1 /chat 接口冒烟测试：验证 SSE 契约与 LangChain 链路的对接
#
# 原则（见《测试任务清单》）：AI 相关调用一律用 mock 隔离，不产生费用、不依赖网络。
# 运行：conda activate knowAgent && pytest tests -q
#
# 说明：多轮记忆的正确性在 test_chat_chain.py 中验证（那里能拿到链路内部的消息组装结果），
#       本文件只关注「接口层契约」：流式帧格式、参数校验、异常降级。

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_module
from app.core.security import DEMO_USER_ID, get_current_user_id_sse
from app.main import create_app


@pytest.fixture()
def client():
    """创建测试用应用实例（每次用例独立，避免状态互相污染）。

    P2-B2：/chat 已接入 JWT 鉴权。这里用依赖覆盖注入固定用户，
    使本文件继续专注验证「SSE 契约」（鉴权本身由 tests/test_auth.py 覆盖），
    否则所有用例都会先被 401 拦下。
    """
    app = create_app()
    app.dependency_overrides[get_current_user_id_sse] = lambda: DEMO_USER_ID
    return TestClient(app)


@pytest.fixture()
def fake_stream(monkeypatch):
    """替换真实的 LangChain 调用链：按固定分片产出文本，并记录收到的入参。

    Returns:
        captured: 列表，收集每次调用的关键入参，用于断言路由与历史透传。
    """
    captured = []

    # 签名需与真实 stream_answer 对齐：P2 起多出 memory_enabled 开关
    # （持久化路径传 False，表示历史由数据库注入、不读写内存记忆）。
    async def _fake(message, conversation_id=None, history=None, meta=None, memory_enabled=True):
        captured.append(
            {
                "message": message,
                "conversation_id": conversation_id,
                "history": history,
                "meta": meta,
            }
        )
        # 模拟 finish_reason 回传：验证元信息能否顺着链路透出给上层
        if isinstance(meta, dict):
            meta["finish_reason"] = "stop"
        for piece in ["你好", "，我是", "知识库助手"]:
            yield piece

    monkeypatch.setattr(chat_module, "stream_answer", _fake)
    return captured


def test_health(client):
    """/health 应返回服务状态与大模型配置情况。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "llm_ready" in body


def test_chat_get_streams_tokens(client, fake_stream):
    """GET /chat 应以 text/event-stream 逐片返回内容，并以 [DONE] 收尾。"""
    with client.stream("GET", "/chat", params={"message": "你好"}) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        # 关闭 nginx 缓冲的关键响应头，缺失会导致前端看不到流式效果
        assert resp.headers.get("x-accel-buffering") == "no"

        frames = [line for line in resp.iter_lines() if line]

    # 首帧为心跳注释，中间为内容帧，最后一帧为结束哨兵
    assert frames[0].startswith(":")
    assert frames[-1] == "data: [DONE]"
    contents = [f for f in frames[1:-1] if f.startswith("data:")]
    assert contents  # 至少有一片内容
    # P2：数据库可用时，首个内容帧是「会话回绑帧」{"type":"conversation","id":...}，
    #     真正的回答分片排在它之后，故取第一个带 content 的帧做断言。
    content_frames = [f for f in contents if '"content"' in f]
    assert content_frames, "未收到任何回答内容帧"
    assert '"content": "你好"' in content_frames[0]


def test_chat_post_streams_tokens(client, fake_stream):
    """POST /chat 与 GET 走同一套流式逻辑。"""
    with client.stream("POST", "/chat", json={"message": "讲讲知识库"}) as resp:
        assert resp.status_code == 200
        frames = [line for line in resp.iter_lines() if line]

    assert frames[-1] == "data: [DONE]"
    # 提问被正确透传给调用链
    assert fake_stream[0]["message"] == "讲讲知识库"


def test_chat_passes_conversation_id_to_chain(client, fake_stream, monkeypatch):
    """P2：数据库可用时，链收到的会话 ID 来自「后端会话」，而非客户端原样字符串。

    P1 行为是客户端传什么就透传什么；P2 接入会话持久化后，无效的
    conversation_id 会被当作「需要新建」，链拿到的是后端会话的真实 UUID。
    这里桩掉会话服务（避免触碰真实库），专注验证「链路 ID = 后端会话 ID」这一契约。
    """
    known_id = "11111111-2222-3333-4444-555555555555"

    class _Conv:
        """最小会话桩：只需提供 id。"""

        id = known_id

    async def fake_get_or_create(dbsession, user_id, conversation_id=None):
        return _Conv()

    async def fake_load_history(dbsession, conversation_id, max_turns, token_budget):
        return []

    async def fake_append(dbsession, conversation_id, user_content, assistant_content):
        return None

    monkeypatch.setattr(chat_module.svc, "get_or_create_conversation", fake_get_or_create)
    monkeypatch.setattr(chat_module.svc, "load_history_window", fake_load_history)
    monkeypatch.setattr(chat_module.svc, "append_turn", fake_append)

    with client.stream(
        "GET", "/chat", params={"message": "你好", "conversation_id": "conv-1"}
    ) as resp:
        list(resp.iter_lines())

    # 链收到的是后端会话 UUID（新建结果），而不是客户端传来的 "conv-1"
    assert fake_stream[0]["conversation_id"] == known_id


def test_chat_forwards_client_history(client, fake_stream, monkeypatch):
    """P2：数据库可用时，历史以数据库为唯一来源，客户端携带的 history 被忽略。

    P1 行为是「客户端传什么历史就注入什么」；P2 接入会话持久化后，
    为避免客户端历史与库内历史重复叠加，DB 路径只认 load_history_window 的结果。
    客户端 history 仅在「数据库不可用、回退内存路径」时才生效。
    """
    db_history = [{"role": "user", "content": "库内历史问题"}]
    known_id = "22222222-3333-4444-5555-666666666666"

    class _Conv:
        """最小会话桩：只需提供 id，避免测试往真实库里写会话。"""

        id = known_id

    async def fake_get_or_create(dbsession, user_id, conversation_id=None):
        return _Conv()

    async def fake_load_history(dbsession, conversation_id, max_turns, token_budget):
        return db_history

    async def fake_append(dbsession, conversation_id, user_content, assistant_content):
        return None

    monkeypatch.setattr(chat_module.svc, "get_or_create_conversation", fake_get_or_create)
    monkeypatch.setattr(chat_module.svc, "load_history_window", fake_load_history)
    monkeypatch.setattr(chat_module.svc, "append_turn", fake_append)

    payload = {
        "message": "那第二点呢",
        "conversation_id": "conv-2",
        # 客户端历史：DB 路径下应被忽略
        "history": [{"role": "user", "content": "客户端历史（应被忽略）"}],
    }
    with client.stream("POST", "/chat", json=payload) as resp:
        list(resp.iter_lines())

    # 链收到的是数据库历史，而不是 payload.history
    assert fake_stream[0]["history"] == db_history


def test_chat_rejects_empty_message(client, fake_stream):
    """空提问应被参数校验拦截（422），不应调用大模型。"""
    resp = client.get("/chat", params={"message": ""})
    assert resp.status_code == 422
    assert fake_stream == []


def test_chat_reports_error_in_stream(client, monkeypatch):
    """链路报错时不应中断连接，而应在流内给出可读提示并正常收尾。"""
    from app.core.errors import LLMNotConfiguredError

    async def _raise(message, conversation_id=None, history=None, meta=None, memory_enabled=True):
        raise LLMNotConfiguredError("DeepSeek API Key 未配置")
        yield  # pragma: no cover —— 仅为让函数成为生成器

    monkeypatch.setattr(chat_module, "stream_answer", _raise)

    with client.stream("GET", "/chat", params={"message": "你好"}) as resp:
        frames = [line for line in resp.iter_lines() if line]

    assert frames[-1] == "data: [DONE]"
    assert any("未配置" in f for f in frames)


def test_chat_warns_when_truncated(client, monkeypatch):
    """回答被 max_tokens 截断时，应追加可读的截断提示（否则用户无法分辨「答完」与「截断」）。"""

    async def _truncated(message, conversation_id=None, history=None, meta=None, memory_enabled=True):
        if isinstance(meta, dict):
            meta["finish_reason"] = "length"
        yield "半截回答"

    monkeypatch.setattr(chat_module, "stream_answer", _truncated)

    with client.stream("GET", "/chat", params={"message": "写一篇长文"}) as resp:
        frames = [line for line in resp.iter_lines() if line]

    body = "\n".join(frames)
    assert "半截回答" in body
    assert "被截断" in body
    assert frames[-1] == "data: [DONE]"
