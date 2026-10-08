# P1-B1 接口冒烟测试：验证 /chat 的 SSE 契约
#
# 原则（见《测试任务清单》）：AI 相关调用一律用 mock 隔离，不产生费用、不依赖网络。
# 运行：conda activate knowAgent && pytest tests -q

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_module
from app.main import create_app
from app.services.memory import get_memory


@pytest.fixture()
def client():
    """创建测试用应用实例（每次用例独立，避免会话记忆互相污染）。"""
    # 清空进程内记忆单例的缓存，保证用例之间互不干扰
    get_memory.cache_clear()
    return TestClient(create_app())


@pytest.fixture()
def fake_stream(monkeypatch):
    """替换真实的 DeepSeek 调用：按固定分片产出文本，并记录收到的入参。

    Returns:
        captured: 列表，收集每次调用时传给模型的 messages，用于断言历史注入。
    """
    captured = []

    async def _fake(messages, settings=None):
        captured.append(messages)
        for piece in ["你好", "，我是", "知识库助手"]:
            yield piece

    monkeypatch.setattr(chat_module, "stream_chat", _fake)
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
    assert '"content": "你好"' in contents[0]


def test_chat_post_streams_tokens(client, fake_stream):
    """POST /chat 与 GET 走同一套流式逻辑。"""
    with client.stream("POST", "/chat", json={"message": "讲讲知识库"}) as resp:
        assert resp.status_code == 200
        frames = [line for line in resp.iter_lines() if line]

    assert frames[-1] == "data: [DONE]"
    # 提问被正确传入模型入参的最后一轮
    assert fake_stream[0][-1]["content"] == "讲讲知识库"


def test_chat_injects_history_on_second_turn(client, fake_stream):
    """第二轮提问应携带第一轮的历史，实现单会话多轮记忆。"""
    with client.stream("GET", "/chat", params={"message": "我叫小明"}) as resp:
        list(resp.iter_lines())
    with client.stream("GET", "/chat", params={"message": "我叫什么"}) as resp:
        list(resp.iter_lines())

    second_turn_messages = fake_stream[1]
    roles = [m["role"] for m in second_turn_messages]
    # system + 上一轮(user/assistant) + 本轮 user
    assert roles[0] == "system"
    assert "user" in roles and "assistant" in roles
    assert second_turn_messages[-1]["content"] == "我叫什么"


def test_chat_rejects_empty_message(client, fake_stream):
    """空提问应被参数校验拦截（422），不应调用大模型。"""
    resp = client.get("/chat", params={"message": ""})
    assert resp.status_code == 422
    assert fake_stream == []


def test_chat_without_api_key_reports_error(client, monkeypatch):
    """未配置 Key 时不应抛异常中断连接，而应在流内给出可读提示并正常收尾。"""
    from app.services import deepseek_client

    async def _raise(messages, settings=None):
        raise deepseek_client.DeepSeekError("DeepSeek API Key 未配置")
        yield  # pragma: no cover —— 仅为让函数成为生成器

    monkeypatch.setattr(chat_module, "stream_chat", _raise)

    with client.stream("GET", "/chat", params={"message": "你好"}) as resp:
        frames = [line for line in resp.iter_lines() if line]

    assert frames[-1] == "data: [DONE]"
    assert any("未配置" in f for f in frames)
