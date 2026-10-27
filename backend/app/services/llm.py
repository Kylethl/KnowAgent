# DeepSeek 大模型实例工厂（P1-A1：LangChain 封装）
# 职责：用 LangChain 官方 DeepSeek 集成（langchain_deepseek.ChatDeepSeek）构造模型对象，
#       把「模型名 / 温度 / 最大 token / 超时 / 流式」等参数集中在此处，业务层不再直接拼 SDK 参数。
#
# 选型说明（对齐《需求清单》第三节：「Agent = LangChain + LangGraph」）：
#   - ChatDeepSeek 是 LangChain 官方维护的 DeepSeek 集成（内部走 OpenAI 兼容协议），
#     相比直接用 ChatOpenAI 兜抄 base_url，语义更明确、后续升级/排障也更省事；
#   - 它同时是一个标准的 LangChain Runnable / BaseChatModel，
#     可以直接用 `prompt | model` 组成 LCEL 链路，P4 接入 RAG 时无需改动模型层。
#
# 替换关系：本模块接管 P1-B1 的 services/deepseek_client.py（httpx 直连实现）。

from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek

from ..config import Settings, get_settings
from ..core.errors import LLMNotConfiguredError
from ..core.logging import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    """构造并返回 DeepSeek 聊天模型单例。

    为什么做成单例：
        模型对象内部持有 HTTP 连接池（async_client / client），
        每次请求都重建会造成连接无法复用、并在并发下产生额外的 TLS 握手开销。

    Returns:
        BaseChatModel：实际类型为 ChatDeepSeek，此处以 LangChain 基类声明，
        便于单测注入 Fake 模型时做类型替换。

    Raises:
        LLMNotConfiguredError: 未配置可用 API Key（含占位假 Key 的情形）。
    """
    cfg: Settings = get_settings()

    # 前置校验：缺少可用 Key 时快速失败，避免发起必然 401 的请求。
    # 注意不要把 Key 本身写进日志，防止密钥泄漏。
    if not cfg.is_deepseek_configured:
        raise LLMNotConfiguredError(
            "DeepSeek API Key 未配置，请在 backend/.env 中填写 DEEPSEEK_API_KEY"
        )

    model = ChatDeepSeek(
        # 对话生成模型，按《需求清单》锁定为 DeepSeek-Flash
        model=cfg.DEEPSEEK_MODEL,
        # DeepSeek 服务地址（OpenAI 兼容）；留可配置项是为了后续接私有化/合规云
        api_base=cfg.DEEPSEEK_BASE_URL,
        # API Key：pydantic SecretStr 要求显式构造，避免被意外打印
        api_key=cfg.DEEPSEEK_API_KEY.strip(),
        # 采样温度：问答场景偏低，保证回答稳定
        temperature=cfg.DEEPSEEK_TEMPERATURE,
        # 单次回答最大 token 数，防止超长输出拖垮连接
        max_tokens=cfg.DEEPSEEK_MAX_TOKENS,
        # 上游调用超时（秒）
        request_timeout=cfg.DEEPSEEK_TIMEOUT,
        # 保持 1 次重试以覆盖偶发网络抖动；
        # 不做更多重试，避免隐藏上游错误并产生重复计费（P7 会做统一的限流与配额控制）
        max_retries=1,
        # 开启流式能力，供上层 astream 逐片段下发
        streaming=True,
    )

    logger.info(
        "初始化 DeepSeek 模型（LangChain ChatDeepSeek）：model=%s，base_url=%s，temperature=%s，max_tokens=%s",
        cfg.DEEPSEEK_MODEL,
        cfg.DEEPSEEK_BASE_URL,
        cfg.DEEPSEEK_TEMPERATURE,
        cfg.DEEPSEEK_MAX_TOKENS,
    )
    return model
