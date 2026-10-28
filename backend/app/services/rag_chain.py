# RAG 生成链（P4-A1：生成链 / P4-B1：检索增强生成）
# ---------------------------------------------------------------------
# 职责：把「检索到的参考资料 + 系统人设 + 多轮历史 + 本轮提问」封装为一条 LangChain 链，
#       并对模型的回答施加「基于资料作答 + 标注引用编号」的约束。
#
# 与 P1 的 chat_chain 的关系：
#   - chat_chain 是「无检索」的通用对话链（P1/P2 沿用，RAG 关闭或降级时仍走它）；
#   - 本模块是「有检索」的增强链，Prompt 多一个 【参考资料】 槽位，
#     并追加引用规则。两者结构一致（system + history + human），
#     因此流式消费逻辑完全复用 chat_chain.stream_answer()，不重复实现。
#
# 为什么把资料放在 system 而不是 human：
#   human 位置保持「纯用户提问」，多轮历史与当前问题语义连续；
#   资料与规则同属「约束」，放在 system 里模型遵从度更高，
#   也不会被后续轮次的历史消息挤散（截断策略只裁历史，不动 system）。
#
# 引用编号约定：
#   参考资料按 [1] [2] [3] 编号，要求模型在对应句子末尾用同样的编号标注来源。
#   编号 → 文档/切片的映射由 services/retriever.build_citations() 产出，
#   P4 功能2 用它渲染引用卡片（本功能先把链条打通）。

from functools import lru_cache
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable

from ..config import get_settings
from ..core.logging import get_logger
from .llm import get_chat_model

logger = get_logger(__name__)

# 无资料时填入 context 槽位的占位文案（让模型明确知道「本次检索为空」而非收到空字符串）
EMPTY_CONTEXT_PLACEHOLDER = "（本次未检索到与问题相关的知识库资料）"

# 引用与作答规则：作为 system prompt 的固定后半段追加在人设之后。
# 措辞刻意强调「不得编造」与「不足时如实说明」，这是 RAG 场景最主要的幻觉防线。
RAG_RULES = """【回答要求】
1. 优先依据下方【参考资料】回答。资料能覆盖的部分，必须严格按资料作答，不得改写事实、不得补充资料之外的具体数字与结论。
2. 引用资料时，在相应句子末尾用方括号标注来源编号，例如「支持 docx 与 md 两种格式[1]」；同一句引用多段资料可写作 [1][3]。
3. 若【参考资料】不足以回答问题，请先明确说明「知识库中未找到相关内容」，再给出你的通用理解，并标注「（以下为通用回答，非知识库内容）」。
4. 不要输出「参考资料」原文全文，只提炼与问题相关的要点；不要提及「编号」「切片」等内部术语之外的技术细节。
5. 使用简洁、准确的中文回答。"""


def compose_rag_system_prompt(base_persona: Optional[str] = None) -> str:
    """拼装 RAG 场景的 system prompt（人设 + 引用规则）。

    Args:
        base_persona: 基础人设（默认取配置的 SYSTEM_PROMPT，与 P1 保持一致）；
            显式传空串时退化为仅含规则。

    Returns:
        供 Prompt 的 {system_prompt} 槽位使用的完整系统提示词。
    """
    cfg = get_settings()
    persona = (base_persona if base_persona is not None else cfg.SYSTEM_PROMPT).strip()
    # 人设与规则之间空一行，便于模型区分「角色」与「硬约束」
    return f"{persona}\n\n{RAG_RULES}" if persona else RAG_RULES


def build_rag_prompt() -> ChatPromptTemplate:
    """构造检索增强的提示词模板。

    模板结构（与 chat_chain.build_prompt 同构，额外多一个 {context} 槽位）：
      - system：人设 + 引用规则 + 【参考资料】正文
      - history：多轮历史（为空自动留空）
      - human：用户本轮提问

    Returns:
        ChatPromptTemplate：可直接用 `|` 与模型串成链路。
    """
    return ChatPromptTemplate.from_messages(
        [
            # context 紧贴 system 末尾：资料属于「本轮约束」，随 system 一起被模型优先遵循
            ("system", "{system_prompt}\n\n【参考资料】\n{context}"),
            # 历史占位保持 optional：首轮对话没有历史时不会被模型以「空占位」拒绝
            MessagesPlaceholder("history", optional=True),
            ("human", "{question}"),
        ]
    )


@lru_cache(maxsize=1)
def get_rag_chain() -> Runnable:
    """构造（并缓存）检索增强生成链。

    不加 StrOutputParser 的原因同 chat_chain：需要保留 AIMessageChunk 的
    finish_reason / usage 元信息，用于判断是否被 max_tokens 截断。

    Returns:
        Runnable：prompt(context) | ChatDeepSeek 组成的 LCEL 链路。

    Raises:
        LLMNotConfiguredError: 未配置可用 API Key（由 get_chat_model 抛出）。
    """
    chain = build_rag_prompt() | get_chat_model()
    logger.info("已构造 LangChain 检索增强链：prompt(context) → DeepSeek")
    return chain


def normalize_context(context: Optional[str]) -> str:
    """把检索上下文规范化为可直接放入 Prompt 的文本。

    Args:
        context: 检索器拼装好的参考资料；None 或空白表示无资料。

    Returns:
        非空的上下文文本：无资料时返回 EMPTY_CONTEXT_PLACEHOLDER，
        避免 Prompt 中出现「【参考资料】」后直接空白导致模型误判为截断。
    """
    text = (context or "").strip()
    return text or EMPTY_CONTEXT_PLACEHOLDER
