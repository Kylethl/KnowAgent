# DeepSeek 调用链（P1-A1：LangChain 封装）
# 职责：把「系统提示词 + 多轮历史 + 当前提问 → DeepSeek 流式生成」封装为一条 LangChain 链路，
#       并接管 A1 要求的「单会话内多轮上下文记忆」。
#
# 链路形态（LCEL）：
#     ChatPromptTemplate(system + history 占位 + human)  →  ChatDeepSeek(流式)
#     流式输出的是 AIMessageChunk，取 .content 即为本段增量文本。
#
# 关于「为什么不用 RunnableWithMessageHistory」：
#     LangChain 1.x 已将该类标记为 deprecated（官方建议改用 LangGraph persistence），
#     在本阶段就引入废弃 API 会给后续升级埋坑。因此这里采用需求文档明示的另一条路径
#     ——**基础 memory + 历史注入**：用 LangChain 标准的 BaseChatMessageHistory 存储会话记忆，
#     由本模块显式「读出历史 → 注入 Prompt → 写回本轮」。
#     语义等价、行为可控（例如客户端中断时不写脏数据），
#     P2 落库后只需替换 langchain_memory.py 的存储后端即可复用本链路。
#
# 替换关系：接管 P1-B1 的 services/deepseek_client.stream_chat()。
#
# P4 扩展（RAG 检索增强）：
#   本模块仍是**唯一的流式消费实现**——检索增强只是「换一条 Prompt 结构不同的链」，
#   stream_answer(..., context=...) 为 None 时走通用链（P1/P2 行为不变），
#   非 None 时走 services/rag_chain.py 的增强链（资料 + 引用规则进 system）。
#   这样既满足《需求清单》「生成链（LangChain）」的要求，也避免流式逻辑被复制两份。

from functools import lru_cache
from typing import Any, AsyncIterator, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable

from ..config import Settings, get_settings
from ..core import egress
from ..core.errors import DeepSeekError, LLMError, LLMNotConfiguredError
from ..core.logging import get_logger
from .langchain_memory import HistoryItem, dicts_to_messages, get_memory_store
from .llm import get_chat_model
from .rag_chain import compose_rag_system_prompt, get_rag_chain, normalize_context

logger = get_logger(__name__)


def build_prompt() -> ChatPromptTemplate:
    """构造对话提示词模板。

    模板包含三段：
      - system：人设与回答约束（P4 的检索增强链在 services/rag_chain.py，
        那里额外携带 【参考资料】 槽位与引用编号规则；本模板保持「无检索」形态）；
      - history：多轮历史占位，为空时自动留空（optional=True，避免空占位被某些模型拒绝）；
      - human：用户本轮提问。

    Returns:
        ChatPromptTemplate：可直接用 `|` 与模型串成链路。
    """
    return ChatPromptTemplate.from_messages(
        [
            ("system", "{system_prompt}"),
            MessagesPlaceholder("history", optional=True),
            ("human", "{question}"),
        ]
    )


@lru_cache(maxsize=1)
def get_chat_chain() -> Runnable:
    """构造（并缓存）DeepSeek 对话链。

    为什么不加 StrOutputParser：
        输出保持为 AIMessage / AIMessageChunk 才能读到 finish_reason、usage 等元信息，
        用于判断是否被 max_tokens 截断（str 输出会把这些元数据丢掉）。

    Returns:
        Runnable：prompt | model 组成的 LCEL 链路。

    Raises:
        LLMNotConfiguredError: 未配置可用 API Key（由 get_chat_model 抛出）。
    """
    chain = build_prompt() | get_chat_model()
    logger.info("已构造 LangChain 对话链：prompt → DeepSeek")
    return chain


async def stream_answer(
    question: str,
    conversation_id: Optional[str] = None,
    history: Optional[List[HistoryItem]] = None,
    meta: Optional[dict] = None,
    memory_enabled: bool = True,
    context: Optional[str] = None,
) -> AsyncIterator[str]:
    """流式调用 DeepSeek 逐片段产出回答，并维护该会话的多轮记忆。

    流程：
      1. 校验 Key 是否可用（不可用则快速失败，避免发起必然 401 的请求）；
      2. 解析历史来源：
         - memory_enabled=True（P1 默认 / 无会话持久化）：走 LangChain 内存记忆
           （读取 / 必要时注入客户端历史 / 写回本轮），保证「单会话多轮上下文」；
         - memory_enabled=False（P2 会话持久化）：历史由外部（数据库）注入，
           本函数不再读写内存记忆，避免「内存 + 数据库」双写导致上下文错乱；
      3. 读出会话历史，与 system / question 一起喂给 LangChain 链：
         - context 为 None（无检索，P1/P2 行为）→ 通用对话链；
         - context 非 None（P4 检索增强）→ 增强链，资料与引用规则进入 system；
      4. 流式产出增量文本；同时把 finish_reason 写入 meta 供上层判断是否被截断；
      5. memory_enabled=True 时，正常结束后把本轮「用户提问 + 助手回答」写回记忆。

    Args:
        question: 用户本轮提问。
        conversation_id: 会话 ID；为空则使用默认会话（仅 memory_enabled=True 时生效）。
        history: 显式携带的历史（客户端注入或数据库加载）；memory_enabled=False 时作为唯一历史来源。
        meta: 可选的可变字典，用于回传元信息（当前写入 finish_reason 与 rag 标记）。
        memory_enabled: 是否启用 LangChain 内存记忆。P2 会话落库后应置 False，由数据库管理历史。
        context: P4 检索增强用的参考资料文本。
            None  表示本轮不启用检索（走 P1/P2 的通用链）；
            空串  表示「检索已执行但无命中」，仍走增强链，由 Prompt 引导模型如实说明。

    Yields:
        模型增量输出的文本片段。

    Raises:
        LLMNotConfiguredError: 未配置可用 API Key。
        DeepSeekError: 上游调用失败（携带 status_code 便于区分鉴权/限流/服务异常）。
    """
    cfg: Settings = get_settings()

    # P7-B2（数据不出域）：在发起任何网络调用之前校验目标域名。
    # strict 模式下若 DeepSeek 地址不在白名单且非内网，这里直接抛
    # DataResidencyViolation（由 API 层转成可读提示），绝不把问题数据发出去。
    # 说明：用 getattr 读 base_url 是为了兼容单测里的配置替身（只提供被断言用到的字段）。
    base_url = (getattr(cfg, "DEEPSEEK_BASE_URL", "") or "").strip()
    if base_url:
        egress.check_outbound(f"{base_url.rstrip('/')}/chat/completions", "llm")

    # 1) 前置校验：无可用 Key 时直接进入异常处理路径，由 API 层转成友好提示
    if not cfg.is_deepseek_configured:
        raise LLMNotConfiguredError(
            "DeepSeek API Key 未配置，请在 backend/.env 中填写 DEEPSEEK_API_KEY"
        )

    # 2) 解析历史来源：内存记忆路径 vs 外部注入路径
    if memory_enabled:
        # P1 默认路径：会话上下文由 LangChain 内存记忆托管
        store = get_memory_store()
        session_history = store.get_session_history(conversation_id)
        session_id = session_history.conversation_id

        # 客户端历史注入（仅在会话为空时生效，避免每轮重复叠加）
        if history:
            await store.seed_history(session_id, history)

        # 历史取自该会话的内存记忆对象（已在上一步注入/裁剪）
        history_messages: List[BaseMessage] = await session_history.aget_messages()
    else:
        # P2 持久化路径：历史由数据库加载并作为参数传入，本函数不再触碰内存记忆，
        # 避免「内存 + 数据库」双写导致下一轮注入重复/错乱的历史。
        session_id = conversation_id or "db"
        history_messages = dicts_to_messages(history) if history else []

    # 3) 组装本轮输入：P4 起按是否带上下文选择「通用链 / 增强链」
    use_rag = context is not None
    inputs: Dict[str, Any] = {
        "history": history_messages,
        "question": question,
    }
    if use_rag:
        # 增强链：人设 + 规则进 system，资料填 context 槽位（无资料时填占位文案）。
        # 先判定「本轮是否真有资料」——它同时决定 context 槽位内容与用哪套规则：
        # 零命中时必须切换到禁止引用编号的规则集，否则模型会凭空标注 [1]，
        # 与前端「知识库未命中」徽标互相矛盾（详见 rag_chain.RAG_RULES_EMPTY 注释）。
        has_context = bool((context or "").strip())
        inputs["context"] = normalize_context(context)
        inputs["system_prompt"] = compose_rag_system_prompt(
            cfg.SYSTEM_PROMPT, has_context=has_context
        )
        if meta is not None:
            # 供上层日志/前端区分「基于知识库作答」与「检索无命中后的通用回答」
            meta["rag"] = True
            meta["rag_has_context"] = has_context
        chain = get_rag_chain()
    else:
        # 通用链：保持 P1 行为完全不变（RAG 关闭或降级时走这里）
        inputs["system_prompt"] = cfg.SYSTEM_PROMPT
        if meta is not None:
            meta["rag"] = False
        chain = get_chat_chain()

    logger.info(
        "开始流式回答（%s）：conversation_id=%s，历史消息数=%d，memory_enabled=%s，资料长度=%d",
        "RAG 增强链" if use_rag else "通用链",
        session_id,
        len(history_messages),
        memory_enabled,
        len(inputs.get("context", "")),
    )

    answer_parts: List[str] = []

    try:
        # 4) 流式产出：AIMessageChunk.content 即本段增量文本
        async for chunk in chain.astream(inputs):
            text = chunk_to_text(chunk)
            read_finish_reason(chunk, meta)
            if text:
                answer_parts.append(text)
                yield text
    except LLMError:
        # 已是本项目统一的异常类型，直接透传给上层
        raise
    except Exception as exc:  # noqa: BLE001 ——统一收敛 SDK/网络的各类异常
        # 注意：asyncio.CancelledError 继承自 BaseException，不会被此处捕获，
        # 因此「客户端中断」不会走到这里，也就不会把半截回答写入记忆。
        raise wrap_llm_error(exc) from exc

    # 5) 正常收尾：memory_enabled=True 时写回本轮对话，使下一轮能引用本轮内容
    full_answer = "".join(answer_parts)
    if memory_enabled and full_answer:
        session_history.add_messages(  # type: ignore[name-defined]  # 仅 memory_enabled=True 分支存在
            [HumanMessage(content=question), AIMessage(content=full_answer)]
        )
    logger.info("流式回答结束：conversation_id=%s，共生成 %d 个字符", session_id, len(full_answer))


def chunk_to_text(chunk: Any) -> str:
    """从 LangChain 输出的消息块中提取纯文本。

    Args:
        chunk: 链的输出；流式场景下为 AIMessageChunk。

    Returns:
        文本片段；无法提取时返回空串。
    """
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    # 部分兼容实现的 content 是内容块列表（多模态场景），P1 只取其中的文本块
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def read_finish_reason(chunk: Any, meta: Optional[dict]) -> None:
    """读取模型结束原因与 token 用量并写入 meta。

    - finish_reason 用于判断是否被 max_tokens 截断；
    - usage 用于 P7 的配额结算（token 维度）与成本看板。

    说明：OpenAI 兼容协议下，usage 通常只在**最后一个** chunk 出现，
    因此这里每收到一个带 usage 的块就覆盖写入 meta，最终值即本轮总用量。

    Args:
        chunk: 链的输出消息块。
        meta: 接收元信息的可变字典；为 None 时跳过。
    """
    if meta is None:
        return
    response_metadata = getattr(chunk, "response_metadata", None) or {}
    finish_reason = response_metadata.get("finish_reason")
    if not finish_reason:
        # 兜底：部分版本把元信息放在 additional_kwargs 里
        finish_reason = (getattr(chunk, "additional_kwargs", None) or {}).get("finish_reason")
    if finish_reason:
        meta["finish_reason"] = finish_reason

    # P7-B1：LangChain 统一把用量放在 usage_metadata（input_tokens / output_tokens）；
    # 个别版本仍保留 response_metadata.token_usage，做一次兼容兜底。
    usage = getattr(chunk, "usage_metadata", None)
    if usage:
        meta["prompt_tokens"] = int(usage.get("input_tokens") or 0)
        meta["completion_tokens"] = int(usage.get("output_tokens") or 0)
    else:
        token_usage = response_metadata.get("token_usage") or {}
        if token_usage:
            meta["prompt_tokens"] = int(token_usage.get("prompt_tokens") or 0)
            meta["completion_tokens"] = int(token_usage.get("completion_tokens") or 0)



def wrap_llm_error(exc: Exception) -> LLMError:
    """把 LangChain / OpenAI SDK 抛出的异常收敛为项目统一的 LLMError。

    Args:
        exc: 原始异常。

    Returns:
        LLMError 子类实例，保留原始信息并尽量提取 HTTP 状态码。
    """
    if isinstance(exc, LLMError):
        return exc

    status_code: Optional[int] = getattr(exc, "status_code", None)
    if status_code is None:
        # openai 的 APIStatusError 把状态码挂在 response 上
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None) if response is not None else None
    return DeepSeekError(f"DeepSeek 调用失败：{exc}", status_code=status_code)
