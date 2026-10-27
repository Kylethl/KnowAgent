# P1-A1 LangChain 调用链单元测试
#
# 原则：用「记录型假模型」替换真实 DeepSeek，不产生费用、不依赖网络。
# 覆盖：链路组装、多轮记忆写入与注入、客户端历史注入语义、截断元信息、异常收敛、Key 校验。
#
# 运行：conda activate knowAgent && pytest tests -q

from typing import Any, Iterator, List, Optional

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

import app.services.chat_chain as chain_module
from app.core.errors import DeepSeekError, LLMNotConfiguredError
from app.services.chat_chain import (
    build_prompt,
    chunk_to_text,
    get_chat_chain,
    read_finish_reason,
    stream_answer,
    wrap_llm_error,
)
from app.services.langchain_memory import get_memory_store


class _FakeAPIStatusError(Exception):
    """模拟 openai 的 APIStatusError：状态码挂在异常对象上。"""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class RecordingFakeModel(BaseChatModel):
    """记录入参并按预设文本逐片段输出的假模型。

    用它替换 services/llm.get_chat_model()，即可在完全离线的情况下
    断言「链路喂给模型的消息」是否正确。
    """

    # 每段回答文本；为空时不产出任何片段
    reply: str = "回答内容"
    # 结束原因：stop（正常）/ length（被 max_tokens 截断）
    finish_reason: str = "stop"
    # 指定状态码时抛出模拟的上游异常
    fail_status: Optional[int] = None
    # 每次调用收到的完整消息列表（含 system / 历史 / 本轮提问）
    calls: List[Any] = []

    @property
    def _llm_type(self) -> str:
        return "recording-fake-model"

    def _record(self, messages: List[BaseMessage]) -> None:
        """保存本次调用的消息快照（避免被后续修改影响）。"""
        self.calls.append(list(messages))

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._record(messages)
        if self.fail_status is not None:
            raise _FakeAPIStatusError("上游服务异常", self.fail_status)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        result = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        content = result.generations[0].message.content or ""
        # 每 2 个字符切成一片，模拟真实的逐个 token 下发
        pieces = [content[i : i + 2] for i in range(0, len(content), 2)] or [""]
        for index, piece in enumerate(pieces):
            # 只在最后一片带上 finish_reason，与 OpenAI 协议行为一致
            metadata = {"finish_reason": self.finish_reason} if index == len(pieces) - 1 else {}
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece, response_metadata=metadata))

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        # 复用同步实现，保证同步/异步两条路径行为一致
        for chunk in self._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk


@pytest.fixture()
def configured(monkeypatch):
    """注入「已配置 Key」的设置，并安装记录型假模型。

    Returns:
        model: 假模型实例，可读取 model.calls 断言链路喂给模型的内容。
    """
    from app.config import Settings

    fake_settings = Settings(DEEPSEEK_API_KEY="sk-test-key-for-unit-tests")
    monkeypatch.setattr(chain_module, "get_settings", lambda: fake_settings)

    model = RecordingFakeModel(reply="你好，我是助手")
    monkeypatch.setattr(chain_module, "get_chat_model", lambda: model)

    # 清空进程内单例缓存，保证用例之间互不影响
    get_chat_chain.cache_clear()
    get_memory_store.cache_clear()
    yield model
    get_chat_chain.cache_clear()
    get_memory_store.cache_clear()


async def drain(question: str, conversation_id: Optional[str] = None, **kwargs: Any) -> str:
    """把流式回答攒成完整字符串（测试辅助函数）。"""
    return "".join([piece async for piece in stream_answer(question, conversation_id, **kwargs)])


# ---------- 链路组装 ----------
def test_build_prompt_has_all_sections():
    """提示词模板应包含 system / history / question 三个输入变量。"""
    prompt = build_prompt()
    assert set(prompt.input_variables) >= {"system_prompt", "question"}

    rendered = prompt.format_messages(
        system_prompt="你是助手",
        history=[HumanMessage(content="历史提问")],
        question="本轮提问",
    )
    assert isinstance(rendered[0], SystemMessage)
    assert rendered[0].content == "你是助手"
    assert rendered[-1].content == "本轮提问"  # 当前提问始终是最后一条
    assert any(msg.content == "历史提问" for msg in rendered)


def test_build_prompt_allows_empty_history():
    """history 为空时应正常渲染（MessagesPlaceholder 声明了 optional=True）。"""
    rendered = build_prompt().format_messages(system_prompt="SYS", history=[], question="Q")
    assert [type(m).__name__ for m in rendered] == ["SystemMessage", "HumanMessage"]


def test_get_chat_chain_uses_injected_model(configured):
    """链路应使用注入的模型构造，且被缓存复用。"""
    chain = get_chat_chain()
    assert chain is not None
    assert get_chat_chain() is chain  # lru_cache 单例语义


# ---------- 多轮记忆 ----------
@pytest.mark.asyncio
async def test_stream_answer_returns_full_text(configured):
    """流式输出的所有片段拼接后应等于完整回答。"""
    assert await drain("你好") == "你好，我是助手"


@pytest.mark.asyncio
async def test_stream_answer_saves_turn_to_memory(configured):
    """正常结束后，本轮 user + assistant 应写入该会话记忆。"""
    await drain("我叫小明", conversation_id="conv-1")

    history = get_memory_store().get_session_history("conv-1").messages
    assert [m.content for m in history] == ["我叫小明", "你好，我是助手"]
    assert isinstance(history[0], HumanMessage)
    assert isinstance(history[1], AIMessage)


@pytest.mark.asyncio
async def test_stream_answer_injects_history_on_second_turn(configured):
    """第二轮调用时，链路应把第一轮的完整对话注入到提示词里（多轮上下文记忆）。"""
    await drain("我叫小明", conversation_id="conv-1")
    await drain("我叫什么", conversation_id="conv-1")

    second_call = configured.calls[1]
    roles = [type(m).__name__ for m in second_call]
    assert roles[0] == "SystemMessage"
    assert second_call[-1].content == "我叫什么"  # 本轮提问在最后
    # 上一轮的 user/assistant 历史被注入
    contents = [m.content for m in second_call]
    assert "我叫小明" in contents
    assert "你好，我是助手" in contents
    # 顺序约束：system 在最前、历史居中、本轮提问在最后
    assert contents.index("我叫什么") == len(contents) - 1


@pytest.mark.asyncio
async def test_stream_answer_isolates_sessions(configured):
    """不同会话的记忆应互相隔离：A 会话的历史不应出现在 B 会话的提示词里。"""
    await drain("会话A的提问", conversation_id="conv-a")
    await drain("会话B的提问", conversation_id="conv-b")

    b_call_contents = [m.content for m in configured.calls[1]]
    assert "会话A的提问" not in b_call_contents
    assert "会话B的提问" in b_call_contents


@pytest.mark.asyncio
async def test_client_history_seeded_only_once(configured):
    """客户端显式历史只注入一次，后续轮次由服务端记忆接管（避免重复叠加）。"""
    client_history = [{"role": "user", "content": "历史提问"}]
    await drain("第二轮", conversation_id="conv-2", history=client_history)
    await drain("第三轮", conversation_id="conv-2", history=client_history)

    final_history = get_memory_store().get_session_history("conv-2").messages
    # 期望：历史提问 + (第二轮, 回答) + (第三轮, 回答) —— 客户端历史没有重复出现
    assert final_history[0].content == "历史提问"
    assert len([m for m in final_history if m.content == "历史提问"]) == 1


# ---------- 截断元信息与文本提取 ----------
@pytest.mark.asyncio
async def test_finish_reason_propagates_to_meta(configured):
    """被 max_tokens 截断时，finish_reason=length 应写入 meta 供上层提示用户。"""
    configured.finish_reason = "length"
    meta: dict = {}
    await drain("写一篇长文", conversation_id="conv-3", meta=meta)

    assert meta["finish_reason"] == "length"


def test_read_finish_reason_falls_back_to_additional_kwargs():
    """部分版本把元信息放在 additional_kwargs，应能兜底读取。"""
    meta: dict = {}
    chunk = AIMessageChunk(content="片段", additional_kwargs={"finish_reason": "length"})
    read_finish_reason(chunk, meta)
    assert meta["finish_reason"] == "length"


def test_read_finish_reason_ignores_none_meta():
    """meta 为 None 时不应抛异常（调用方可不关心元信息）。"""
    read_finish_reason(AIMessageChunk(content="x", response_metadata={"finish_reason": "stop"}), None)


def test_chunk_to_text_handles_str_and_blocks():
    """文本块应能同时处理纯字符串与多模态内容块列表。"""
    assert chunk_to_text(AIMessageChunk(content="abc")) == "abc"
    block_message = AIMessageChunk(
        content=[{"type": "text", "text": "你好"}, {"type": "image_url", "image_url": "..."}]
    )
    assert chunk_to_text(block_message) == "你好"
    assert chunk_to_text(object()) == ""


# ---------- 异常收敛与配置校验 ----------
@pytest.mark.asyncio
async def test_stream_answer_wraps_upstream_error(configured):
    """上游异常应收敛为 DeepSeekError，并尽量保留 HTTP 状态码便于区分鉴权/限流。"""
    configured.fail_status = 429

    with pytest.raises(DeepSeekError) as exc_info:
        await drain("你好", conversation_id="conv-4")

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_stream_answer_requires_api_key(monkeypatch):
    """未配置可用 Key（含占位假 Key）时应快速失败，且不发起任何上游请求。"""
    from app.config import Settings

    monkeypatch.setattr(
        chain_module, "get_settings", lambda: Settings(DEEPSEEK_API_KEY="sk-REPLACE_ME")
    )
    get_memory_store.cache_clear()

    with pytest.raises(LLMNotConfiguredError):
        await drain("你好", conversation_id="conv-nokey")


def test_wrap_llm_error_keeps_project_errors():
    """已是本项目异常类型的错误不应被二次包装，避免丢失语义。"""
    original = DeepSeekError("原始错误", status_code=500)
    assert wrap_llm_error(original) is original


def test_wrap_llm_error_extracts_status_from_response():
    """状态码挂在 response 上时也应被提取出来。"""

    class _Response:
        status_code = 401

    class _SDKError(Exception):
        @property
        def response(self):
            return _Response()

    wrapped = wrap_llm_error(_SDKError("鉴权失败"))
    assert isinstance(wrapped, DeepSeekError)
    assert wrapped.status_code == 401
