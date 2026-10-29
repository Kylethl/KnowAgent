# P4 功能1「RAG 增强回答可见」测试
# ---------------------------------------------------------------------
# 覆盖三层：
#   1. 检索器内部逻辑（词项抽取 / 覆盖率 / 重排 / 去重 / 上下文拼装 / 引用映射）；
#   2. 缓存层（内存回退的 TTL 与 LRU；Redis 不可用不影响结果）；
#   3. 接口契约（/chat 下发 retrieval 帧、把上下文注入生成链、各类降级不阻断对话）。
#
# 原则（见《测试任务清单》）：AI/向量接口一律 mock 隔离，不产生费用、不依赖网络；
# 缓存统一强制走内存后端，避免受本机 Redis 状态影响。

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_module
import app.services.retriever as retriever
from app.core.security import DEMO_USER_ID, get_current_user_id_sse
from app.main import create_app
from app.services.rag_chain import EMPTY_CONTEXT_PLACEHOLDER, compose_rag_system_prompt, normalize_context
from app.services.retrieval_cache import RetrievalCache, reset_retrieval_cache
from app.services.retriever import (
    MODE_DISABLED,
    MODE_FALLBACK,
    MODE_NO_HIT,
    MODE_RAG,
    RetrievalResult,
    RetrievedChunk,
    build_citations,
    build_context,
    dedupe_chunks,
    extract_query_terms,
    keyword_coverage,
    rerank,
)

# =====================================================================
# 公共桩与工具
# =====================================================================


def make_chunk(content: str, score: float = 0.5, heading: str = "", doc_title: str = "文档A") -> RetrievedChunk:
    """构造一个测试用切片（只填断言关心的字段，其余给稳定默认值）。"""
    return RetrievedChunk(
        chunk_id=f"chunk-{abs(hash(content)) % 10000}",
        doc_id="doc-1",
        doc_title=doc_title,
        file_type="markdown",
        chunk_index=0,
        content=content,
        heading=heading,
        score=score,
        vector_score=score,
    )


class _FakeSettings:
    """最小化配置桩：只暴露检索器用到的 RAG 字段。"""

    RAG_ENABLED = True
    RAG_TOP_K = 5
    RAG_CANDIDATE_K = 20
    RAG_SIMILARITY_THRESHOLD = 0.3
    RAG_MAX_CONTEXT_CHARS = 3000
    RAG_RERANK_ENABLED = True
    RAG_CACHE_ENABLED = True
    RAG_CACHE_TTL = 300
    RAG_CACHE_MAX_ITEMS = 4
    RAG_CACHE_KEY_PREFIX = "rag:retrieval:v1"
    REDIS_CACHE_URL = "redis://localhost:6379/2"
    SYSTEM_PROMPT = "你是知识库助手。"

    # 通过 is_rag_ready 控制「检索是否可用」
    def __init__(self, ready: bool = True) -> None:
        self._ready = ready

    @property
    def is_rag_ready(self) -> bool:
        return self._ready


@pytest.fixture()
def memory_cache():
    """把全局缓存替换为「强制内存后端」，避免测试依赖本机 Redis。"""
    cache = reset_retrieval_cache(force_memory=True)
    yield cache
    # 用例结束后复位，避免强制内存状态泄漏到其他测试
    reset_retrieval_cache()


# =====================================================================
# 一、词项抽取与覆盖率
# =====================================================================


def test_extract_query_terms_keeps_cjk_bigrams_and_drops_stopwords():
    """中文按 2-gram 切分，英文按词切分，疑问/停用词应被剔除。"""
    terms = extract_query_terms("支持哪些文档格式？docx")

    # 常见疑问词不进词表（否则覆盖率会被拉高）
    assert "哪些" not in terms
    # 有效 2-gram 存在
    assert "文档" in terms
    assert "格式" in terms
    # 英文词按整体保留
    assert "docx" in terms


def test_extract_query_terms_handles_single_char_and_empty():
    """单字查询保留单字；空查询返回空集合（后续覆盖率恒为 0）。"""
    assert extract_query_terms("钱") == {"钱"}
    assert extract_query_terms("   ") == set()


def test_keyword_coverage_counts_hits():
    """覆盖率 = 命中词项数 / 词项总数。"""
    terms = {"文档", "格式", "部署"}
    # 命中 2/3
    assert keyword_coverage(terms, "支持哪些文档格式") == pytest.approx(2 / 3)
    # 无命中
    assert keyword_coverage(terms, "完全无关的内容") == 0.0
    # 空词表：不应除零
    assert keyword_coverage(set(), "任意文本") == 0.0


# =====================================================================
# 二、重排与去重
# =====================================================================


def test_rerank_prefers_semantic_similarity():
    """语义相似度更高的切片应排在前面（关键词权重不应反客为主）。"""
    semantic_high = make_chunk("完全不含查询词的正文", score=0.8)
    keyword_high = make_chunk("支持 文档 格式 部署", score=0.35)

    result = rerank("支持哪些文档格式", [keyword_high, semantic_high], top_k=2, enabled=True)

    assert result[0] is semantic_high
    # 综合分被写回 score，且不超过 1.0
    assert 0 < result[0].score <= 1.0


def test_rerank_heading_hit_adds_bonus():
    """两个切片语义分相同，章节标题命中查询词的应排前。"""
    with_heading = make_chunk("正文一", score=0.5, heading="部署步骤")
    without_heading = make_chunk("正文二", score=0.5, heading="其他章节")

    result = rerank("部署", [without_heading, with_heading], top_k=2, enabled=True)

    assert result[0] is with_heading
    assert result[0].score > result[1].score


def test_rerank_disabled_keeps_vector_order():
    """关闭重排时完全按向量相似度排序，不做关键词干预。"""
    low = make_chunk("支持 文档 格式", score=0.2)
    high = make_chunk("无关内容", score=0.9)

    result = rerank("支持文档格式", [low, high], top_k=2, enabled=False)

    assert [c.score for c in result] == [0.9, 0.2]


def test_rerank_respects_top_k_and_empty_input():
    """top_k 截断生效；空输入与非法 top_k 返回空列表。"""
    chunks = [make_chunk(f"内容{i}", score=0.9 - i * 0.1) for i in range(5)]
    assert len(rerank("内容", chunks, top_k=2)) == 2
    assert rerank("内容", [], top_k=3) == []
    assert rerank("内容", chunks, top_k=0) == []


def test_dedupe_chunks_merges_identical_content():
    """重复上传的同一段落只保留得分最高的一条，避免占满 Top-K 名额。"""
    a = make_chunk("本节描述部署步骤", score=0.6, doc_title="部署手册.md")
    a_dup = make_chunk("本节描述部署步骤", score=0.59, doc_title="部署手册副本.md")
    b = make_chunk("另一段内容", score=0.4)

    unique = dedupe_chunks([a, a_dup, b])

    assert len(unique) == 2
    assert unique[0] is a
    # 空白差异也应视为同一内容
    c = make_chunk("本节描述   部署步骤", score=0.3)
    assert len(dedupe_chunks([a, c])) == 1


# =====================================================================
# 三、上下文拼装与引用映射
# =====================================================================


def test_build_context_has_numbered_headers():
    """上下文按 [1][2] 编号，并带上文档名与章节，便于模型标注引用。"""
    chunks = [
        make_chunk("问：支持哪些格式？答：docx 与 md。", heading="常见问题", doc_title="产品说明.md"),
        make_chunk("先启动数据库再启动后端。", doc_title="部署手册.md"),
    ]

    context = build_context(chunks, max_chars=1000)

    assert context.startswith("[1] 来源：《产品说明.md》 · 章节：常见问题")
    assert "[2] 来源：《部署手册.md》" in context
    # 无章节的切片不应出现空的「章节：」
    assert "章节：\n" not in context


def test_build_context_respects_char_budget():
    """超出字符预算的切片整条丢弃（不截半条，避免句子被切断误导模型）。"""
    chunks = [make_chunk("甲" * 100, doc_title="A"), make_chunk("乙" * 100, doc_title="B")]

    context = build_context(chunks, max_chars=120)

    # 预算只能容纳首条
    assert "《A》" in context
    assert "《B》" not in context


def test_build_context_empty_cases():
    """无切片或预算为 0 时返回空串（上层据此走「无资料」Prompt）。"""
    assert build_context([], max_chars=100) == ""
    assert build_context([make_chunk("内容")], max_chars=0) == ""


def test_build_citations_maps_index_and_snippet():
    """引用列表序号与 Prompt 编号对齐，片段超长时截断并补省略号。"""
    long_text = "长" * 200
    chunks = [make_chunk(long_text, score=0.7, heading="章节", doc_title="文档.md")]

    citations = build_citations(chunks)

    assert citations[0]["index"] == 1
    assert citations[0]["doc_id"] == "doc-1"
    assert citations[0]["doc_title"] == "文档.md"
    assert citations[0]["heading"] == "章节"
    assert citations[0]["snippet"].endswith("…")
    assert len(citations[0]["snippet"]) <= 121


# =====================================================================
# 四、缓存层（内存回退）
# =====================================================================


@pytest.mark.asyncio
async def test_memory_cache_roundtrip(memory_cache):
    """内存后端可正常读写；未命中的键返回 None。"""
    assert await memory_cache.get("k1") is None
    await memory_cache.set("k1", [{"chunk_id": "c1"}])
    assert await memory_cache.get("k1") == [{"chunk_id": "c1"}]
    assert memory_cache.backend == "memory"


@pytest.mark.asyncio
async def test_memory_cache_lru_eviction(monkeypatch):
    """超过容量上限时淘汰最久未使用的条目（LRU）。"""
    # 容量设 2：写入 k1,k2,k3 后 k1 应被淘汰
    cache = RetrievalCache(force_memory=True)
    cache._memory._max_items = 2

    await cache.set("k1", [{"i": 1}])
    await cache.set("k2", [{"i": 2}])
    # 访问一次 k1，使其成为最近使用
    await cache.get("k1")
    await cache.set("k3", [{"i": 3}])

    assert await cache.get("k2") is None
    assert await cache.get("k1") == [{"i": 1}]
    assert await cache.get("k3") == [{"i": 3}]


@pytest.mark.asyncio
async def test_cache_disabled_returns_none(monkeypatch):
    """RAG_CACHE_ENABLED=False 时读写都不生效（读取恒为 None）。"""
    cache = RetrievalCache(force_memory=True)
    cache._enabled = False

    await cache.set("k1", [{"i": 1}])

    assert await cache.get("k1") is None


@pytest.mark.asyncio
async def test_invalidate_clears_memory(memory_cache):
    """invalidate() 应清空内存缓存（Redis 不可用时也不报错）。"""
    await memory_cache.set("k1", [{"i": 1}])
    removed = await memory_cache.invalidate()

    assert removed >= 1
    assert await memory_cache.get("k1") is None


# =====================================================================
# 五、检索主流程（mock 向量接口与向量查询）
# =====================================================================


@pytest.mark.asyncio
async def test_retrieve_chunks_populates_cache_and_reuses_it(monkeypatch, memory_cache):
    """首次检索写缓存；第二次同问题直接命中缓存，不再调用向量接口与查询。"""
    calls = {"embed": 0, "search": 0}

    async def fake_embed(query: str):
        calls["embed"] += 1
        return [0.1] * 1024

    async def fake_search(db, query_vector, limit, threshold):
        calls["search"] += 1
        return [make_chunk("答案正文", score=0.8, doc_title="产品说明.md")]

    monkeypatch.setattr(retriever.embedding_mod, "embed_query", fake_embed)
    monkeypatch.setattr(retriever, "search_chunks", fake_search)

    first = await retriever.retrieve_chunks("支持哪些格式", db=AsyncMock())
    second = await retriever.retrieve_chunks("支持哪些格式", db=AsyncMock())

    assert first.mode == MODE_RAG
    assert first.cached is False
    assert second.cached is True
    # 缓存生效：向量接口与向量查询各只调用一次
    assert calls == {"embed": 1, "search": 1}
    # 缓存还原后上下文仍可正常拼装
    assert "答案正文" in second.context


@pytest.mark.asyncio
async def test_retrieve_chunks_returns_no_hit_when_empty(monkeypatch, memory_cache):
    """召回为空时返回 no_hit，并给出可读原因。"""

    async def fake_embed(query: str):
        return [0.1] * 1024

    async def fake_search(db, query_vector, limit, threshold):
        return []

    monkeypatch.setattr(retriever.embedding_mod, "embed_query", fake_embed)
    monkeypatch.setattr(retriever, "search_chunks", fake_search)

    result = await retriever.retrieve_chunks("今天天气如何", db=AsyncMock())

    assert result.mode == MODE_NO_HIT
    assert result.hit_count == 0
    assert result.context == ""
    assert "未找到" in (result.reason or "")


# =====================================================================
# 六、降级语义（retrieve_for_chat 永不抛异常）
# =====================================================================


@pytest.mark.asyncio
async def test_retrieve_for_chat_disabled_without_db(monkeypatch, memory_cache):
    """无数据库会话时直接标记「未启用」，不执行检索。"""
    monkeypatch.setattr(retriever, "get_settings", lambda: _FakeSettings(ready=True))

    result = await retriever.retrieve_for_chat("任意问题", None)

    assert result.mode == MODE_DISABLED
    assert "数据库" in (result.reason or "")


@pytest.mark.asyncio
async def test_retrieve_for_chat_disabled_when_rag_not_ready(monkeypatch, memory_cache):
    """配置层面不可用（RAG 关闭 / 无可用向量模型）时标记未启用。"""
    monkeypatch.setattr(retriever, "get_settings", lambda: _FakeSettings(ready=False))

    result = await retriever.retrieve_for_chat("任意问题", AsyncMock())

    assert result.mode == MODE_DISABLED
    assert result.hit_count == 0


@pytest.mark.asyncio
async def test_retrieve_for_chat_falls_back_on_error(monkeypatch, memory_cache):
    """检索过程异常（DB/向量接口）时降级为 fallback，绝不向上抛。"""
    monkeypatch.setattr(retriever, "get_settings", lambda: _FakeSettings(ready=True))

    async def boom(*args, **kwargs):
        raise RuntimeError("向量服务不可用")

    monkeypatch.setattr(retriever, "retrieve_chunks", boom)

    result = await retriever.retrieve_for_chat("任意问题", AsyncMock())

    assert result.mode == MODE_FALLBACK
    assert "向量服务不可用" in (result.reason or "")


def test_retrieval_result_meta_payload_shape():
    """下发给前端的元信息结构固定：类型 + 模式 + 命中数 + 可选项。"""
    payload = RetrievalResult(mode=MODE_RAG, chunks=[make_chunk("x")], elapsed_ms=12).meta_payload()

    assert payload["type"] == "retrieval"
    assert payload["mode"] == MODE_RAG
    assert payload["count"] == 1
    assert payload["elapsed_ms"] == 12
    # 正常命中不应带 reason
    assert "reason" not in payload


# =====================================================================
# 七、生成链分支（chat_chain.stream_answer 的 RAG 选择逻辑）
# =====================================================================


class _FakeChunk:
    """模拟 AIMessageChunk：只需 content 与 response_metadata 两个字段。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.response_metadata = {"finish_reason": "stop"}


class _FakeChain:
    """记录入参并产出固定分片的假链。"""

    def __init__(self, store: dict) -> None:
        self._store = store

    async def astream(self, inputs):
        self._store["inputs"] = inputs
        for piece in ["答案", "[1]"]:
            yield _FakeChunk(piece)


class _LLMSettings:
    """生成链所需的配置桩（仅暴露 chat_chain 读取的字段）。"""

    SYSTEM_PROMPT = "你是知识库助手。"
    is_deepseek_configured = True


@pytest.mark.asyncio
async def test_stream_answer_uses_rag_chain_when_context_given(monkeypatch):
    """传入 context 时应走增强链，system 含引用规则、context 槽位为检索资料。"""
    import app.services.chat_chain as chat_chain

    store: dict = {}
    monkeypatch.setattr(chat_chain, "get_settings", lambda: _LLMSettings())
    monkeypatch.setattr(chat_chain, "get_rag_chain", lambda: _FakeChain(store))

    meta: dict = {}
    parts = [
        p
        async for p in chat_chain.stream_answer(
            "支持哪些格式", history=[], meta=meta, memory_enabled=False, context="[1] 支持 docx 与 md"
        )
    ]

    assert "".join(parts) == "答案[1]"
    # Prompt 输入：资料进入 context 槽位，人设进入 system_prompt
    assert store["inputs"]["context"] == "[1] 支持 docx 与 md"
    assert "参考资料" in store["inputs"]["system_prompt"]
    # meta 标记本轮为 RAG 且有资料
    assert meta["rag"] is True
    assert meta["rag_has_context"] is True


@pytest.mark.asyncio
async def test_stream_answer_uses_generic_chain_without_context(monkeypatch):
    """不传 context（None）时保持 P1 行为：走通用链，不出现参考资料槽位。"""
    import app.services.chat_chain as chat_chain

    store: dict = {}
    monkeypatch.setattr(chat_chain, "get_settings", lambda: _LLMSettings())
    monkeypatch.setattr(chat_chain, "get_chat_chain", lambda: _FakeChain(store))

    meta: dict = {}
    _ = [p async for p in chat_chain.stream_answer("你好", history=[], meta=meta, memory_enabled=False)]

    assert "context" not in store["inputs"]
    assert meta["rag"] is False


@pytest.mark.asyncio
async def test_stream_answer_marks_empty_context_as_rag(monkeypatch):
    """context 为空串（检索无命中）仍走增强链，且 meta 标记「无资料」。"""
    import app.services.chat_chain as chat_chain

    store: dict = {}
    monkeypatch.setattr(chat_chain, "get_settings", lambda: _LLMSettings())
    monkeypatch.setattr(chat_chain, "get_rag_chain", lambda: _FakeChain(store))

    meta: dict = {}
    _ = [
        p
        async for p in chat_chain.stream_answer(
            "今天天气", history=[], meta=meta, memory_enabled=False, context=""
        )
    ]

    # 空上下文被替换为占位文案，避免 Prompt 出现空白资料区
    assert store["inputs"]["context"] == EMPTY_CONTEXT_PLACEHOLDER
    assert meta["rag"] is True
    assert meta["rag_has_context"] is False


# =====================================================================
# 八、Prompt 组装（RAG 约束）
# =====================================================================


def test_compose_rag_system_prompt_contains_rules():
    """RAG system prompt 必须包含「基于资料作答」与「引用编号」两条硬约束。"""
    prompt = compose_rag_system_prompt("你是知识库助手。")

    assert "你是知识库助手。" in prompt
    assert "参考资料" in prompt
    assert "编号" in prompt
    # 防幻觉要求：资料不足时必须如实说明
    assert "未找到" in prompt


def test_normalize_context_never_empty():
    """无资料时填入占位文案，避免 Prompt 出现空白参考资料区。"""
    assert normalize_context("") == EMPTY_CONTEXT_PLACEHOLDER
    assert normalize_context(None) == EMPTY_CONTEXT_PLACEHOLDER
    assert normalize_context("  [1] 资料正文 ") == "[1] 资料正文"


def test_compose_rag_system_prompt_forbids_citation_without_context():
    """零命中时必须切换到「禁止引用编号」的规则集。

    回归用例：曾经无论有无资料都用同一套 RAG_RULES，其中的
    「引用资料时标注 [n]」是正向指令，模型在资料为空时照样照做，
    产出「徽标显示未命中、正文却带 [1]」的自相矛盾回答。
    """
    prompt = compose_rag_system_prompt("你是知识库助手。", has_context=False)

    # 人设仍在
    assert "你是知识库助手。" in prompt
    # 明确禁止编号，而不是「省略规则」——省略仍会被多轮历史模仿
    assert "不要输出" in prompt and "引用编号" in prompt
    # 不得再出现正向示范（「……docx 与 md 两种格式[1]」这种例句会被模型当模板抄）
    assert "两种格式[1]" not in prompt
    # 仍要求如实说明未命中
    assert "未找到" in prompt


def test_compose_rag_system_prompt_keeps_citation_rules_with_context():
    """有资料时保持原有引用规则（标注 [n] + 依据资料作答）。"""
    prompt = compose_rag_system_prompt("你是知识库助手。", has_context=True)

    assert "两种格式[1]" in prompt
    # 不该出现 no-hit 分支特有的那句禁令
    assert "这类引用编号" not in prompt


@pytest.mark.asyncio
async def test_stream_answer_switches_rules_on_empty_context(monkeypatch):
    """端到端串联：context 为空串时，送进链路的 system_prompt 必须是不带引用编号的那套。"""
    import app.services.chat_chain as chat_chain

    store: dict = {}
    monkeypatch.setattr(chat_chain, "get_settings", lambda: _LLMSettings())
    monkeypatch.setattr(chat_chain, "get_rag_chain", lambda: _FakeChain(store))

    _ = [
        p
        async for p in chat_chain.stream_answer(
            "你可以做什么", history=[], meta={}, memory_enabled=False, context=""
        )
    ]

    system_prompt = store["inputs"]["system_prompt"]
    assert "引用编号" in system_prompt      # 已切到 no-hit 规则
    assert "两种格式[1]" not in system_prompt
    assert store["inputs"]["context"] == EMPTY_CONTEXT_PLACEHOLDER


def test_rag_prompt_renders_context_into_system_message():
    """模板渲染后，资料正文应出现在 system 消息里，human 消息只保留提问。"""
    from app.services.rag_chain import build_rag_prompt

    messages = build_rag_prompt().format_messages(
        system_prompt="人设", context="[1] 资料正文", history=[], question="提问"
    )

    system_text = messages[0].content
    assert "【参考资料】" in system_text
    assert "[1] 资料正文" in system_text
    assert messages[-1].content == "提问"


# =====================================================================
# 十、向量时效性（检测 mock 向量 / 换模型后的过期向量）
# ---------------------------------------------------------------------
# 背景：embedding 未配置真实 Key 时写入的是 mock 伪向量，无语义；
#       更换模型后新旧向量也不在同一语义空间。两者都会让检索「看似可用、实则乱召回」，
#       因此需要记录入库时的模型签名并在接口层比对。
# =====================================================================


class _EmbeddingSettings:
    """embedding 签名判定所需的配置桩。"""

    QWEN_EMBEDDING_MODEL = "qwen3.7-text-embedding-flash"

    def __init__(self, configured: bool) -> None:
        self._configured = configured

    @property
    def is_qwen_configured(self) -> bool:
        return self._configured


def test_current_signature_is_model_name_when_configured(monkeypatch):
    """配置了真实 Key 时，签名是模型名（用于与库内记录比对）。"""
    import app.services.embedding as embedding_mod

    monkeypatch.setattr(embedding_mod, "get_settings", lambda: _EmbeddingSettings(True))

    assert embedding_mod.current_embedding_signature() == "qwen3.7-text-embedding-flash"


def test_current_signature_is_mock_when_not_configured(monkeypatch):
    """未配置 Key（走 mock 向量）时签名固定为 "mock"，便于后台一眼识别伪向量。"""
    import app.services.embedding as embedding_mod

    monkeypatch.setattr(embedding_mod, "get_settings", lambda: _EmbeddingSettings(False))

    assert embedding_mod.current_embedding_signature() == embedding_mod.MOCK_SIGNATURE


def test_embedding_is_stale_rules(monkeypatch):
    """过期判定：空记录→需重入库；签名一致→不过期；模型/mock 不一致→过期。"""
    import app.services.embedding as embedding_mod

    monkeypatch.setattr(embedding_mod, "get_settings", lambda: _EmbeddingSettings(True))
    current = "qwen3.7-text-embedding-flash"

    # 历史数据未记录模型：无法确认，按需重新入库处理（重入库一次即自愈）
    assert embedding_mod.embedding_is_stale(None) is True
    assert embedding_mod.embedding_is_stale("") is True
    # 与当前一致：不过期
    assert embedding_mod.embedding_is_stale(current) is False
    # 旧模型 / mock 向量：过期
    assert embedding_mod.embedding_is_stale("text-embedding-v4") is True
    assert embedding_mod.embedding_is_stale(embedding_mod.MOCK_SIGNATURE) is True


def test_embedding_not_stale_when_both_mock(monkeypatch):
    """当前也是 mock 模式、库里也是 mock：签名一致，不算「过期」（只是检索无意义）。"""
    import app.services.embedding as embedding_mod

    monkeypatch.setattr(embedding_mod, "get_settings", lambda: _EmbeddingSettings(False))

    assert embedding_mod.embedding_is_stale(embedding_mod.MOCK_SIGNATURE) is False


# =====================================================================
# 十一、阈值标定脚本的纯函数（不发起网络请求）
# =====================================================================


def _load_calibrate_module():
    """按文件路径加载 scripts/calibrate_rag_threshold.py（该目录不是包）。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "calibrate_rag_threshold.py"
    spec = importlib.util.spec_from_file_location("calibrate_rag_threshold", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_calibrate_cosine_and_percentile():
    """余弦相似度与分位数的基本正确性（标定结论依赖它们）。"""
    mod = _load_calibrate_module()

    # 同向向量相似度为 1，正交为 0，零向量不除零
    assert mod.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert mod.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert mod.cosine([0, 0], [1, 1]) == 0.0

    values = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert mod.percentile(values, 0.5) == pytest.approx(0.3)
    assert mod.percentile(values, 0.0) == pytest.approx(0.1)
    assert mod.percentile(values, 1.0) == pytest.approx(0.5)
    # 空样本不抛异常
    assert mod.percentile([], 0.95) == 0.0


def test_calibrate_parse_queries_handles_fullwidth_separator():
    """问句解析需兼容中文全角分号（中文输入法下极常见）。"""
    mod = _load_calibrate_module()

    queries = mod.parse_queries("支持哪些格式？；怎么部署; ; 部署步骤 ", None)

    assert queries == ["支持哪些格式？", "怎么部署", "部署步骤"]


# =====================================================================
# 十二、/chat 接口契约（retrieval 帧 + 上下文注入 + 降级）
# =====================================================================


class _AsyncSessionCtx:
    """让桩后的 SessionLocal() 支持 async with。"""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


@pytest.fixture()
def client():
    """测试用应用，覆盖 SSE 鉴权依赖（鉴权本身由 test_auth.py 覆盖）。"""
    app = create_app()
    app.dependency_overrides[get_current_user_id_sse] = lambda: DEMO_USER_ID
    return TestClient(app)


def _install_chat_stubs(monkeypatch, retrieval: RetrievalResult, captured: dict) -> None:
    """装配 /chat 的数据库路径桩：会话服务、检索、生成链全部替换为可控实现。"""
    db = AsyncMock()
    monkeypatch.setattr(chat_module, "SessionLocal", lambda: _AsyncSessionCtx(db))

    async def fake_get_or_create(dbsession, user_id, conversation_id=None):
        conv = AsyncMock()
        conv.id = "11111111-1111-1111-1111-111111111111"
        return conv

    async def fake_load_history(dbsession, conversation_id, max_turns, token_budget):
        return []

    async def fake_append(dbsession, conversation_id, user_content, assistant_content, citations=None):
        captured["appended"] = (user_content, assistant_content)
        # P4-B2：引用随助手消息落库
        captured["citations_saved"] = citations

    async def fake_stream(message, conversation_id=None, history=None, meta=None,
                           memory_enabled=True, context=None):
        # 记录 RAG 上下文是否透传到生成链
        captured["context"] = context
        for piece in ["回答", "内容"]:
            yield piece

    # 记录检索是否被调用（默认必须调用一次）
    async def fake_retrieve(query, db_session):
        captured["retrieval_query"] = query
        return retrieval

    monkeypatch.setattr(chat_module.svc, "get_or_create_conversation", fake_get_or_create)
    monkeypatch.setattr(chat_module.svc, "load_history_window", fake_load_history)
    monkeypatch.setattr(chat_module.svc, "append_turn", fake_append)
    monkeypatch.setattr(chat_module, "stream_answer", fake_stream)
    monkeypatch.setattr(chat_module, "retrieve_for_chat", fake_retrieve)


def _find_frame(frames, frame_type: str) -> dict:
    """从 SSE 帧中找出指定 type 的 JSON 帧（找不到直接断言失败）。"""
    for f in frames:
        if not f.startswith("data: "):
            continue
        try:
            obj = json.loads(f[len("data: "):])
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("type") == frame_type:
            return obj
    raise AssertionError(f"未找到 type={frame_type} 的帧；实际帧：{frames}")


def test_chat_emits_retrieval_frame_and_injects_context(client, monkeypatch):
    """命中知识库时：下发 retrieval 帧（mode=rag,count=N），并把上下文传给生成链。"""
    captured: dict = {}
    chunks = [make_chunk("问：支持哪些格式？答：docx 与 md。", score=0.8)]
    retrieval = RetrievalResult(
        mode=MODE_RAG,
        chunks=chunks,
        context="[1] 来源：《产品说明.md》\n问：支持哪些格式？答：docx 与 md。",
        elapsed_ms=42,
    )
    _install_chat_stubs(monkeypatch, retrieval, captured)

    with client.stream("GET", "/chat", params={"message": "支持哪些格式"}) as resp:
        frames = [line for line in resp.iter_lines() if line]

    frame = _find_frame(frames, "retrieval")
    assert frame["mode"] == MODE_RAG
    assert frame["count"] == 1
    # 上下文被注入生成链（内容包含检索到的资料）
    assert "docx" in (captured.get("context") or "")
    # 流仍以 [DONE] 正常收尾，且本轮落库
    assert frames[-1] == "data: [DONE]"
    assert captured.get("appended") == ("支持哪些格式", "回答内容")


def test_chat_retrieval_frame_before_content(client, monkeypatch):
    """retrieval 帧必须在首个正文分片之前下发，前端才能先渲染检索状态。"""
    captured: dict = {}
    _install_chat_stubs(
        monkeypatch,
        RetrievalResult(mode=MODE_NO_HIT, reason="知识库中未找到相关内容"),
        captured,
    )

    with client.stream("GET", "/chat", params={"message": "今天天气"}) as resp:
        frames = [line for line in resp.iter_lines() if line]

    idx_retrieval = next(
        i for i, f in enumerate(frames) if f.startswith("data: ") and '"retrieval"' in f
    )
    idx_content = next(
        i for i, f in enumerate(frames) if f.startswith("data: ") and '"content"' in f
    )
    assert idx_retrieval < idx_content


def test_chat_passes_empty_context_when_no_hit(client, monkeypatch):
    """无命中时仍走增强链（context 为空串而非 None），由 Prompt 引导模型如实说明。"""
    captured: dict = {}
    _install_chat_stubs(monkeypatch, RetrievalResult(mode=MODE_NO_HIT), captured)

    with client.stream("GET", "/chat", params={"message": "今天天气"}) as resp:
        list(resp.iter_lines())

    assert captured.get("context") == ""


def test_chat_continues_when_retrieval_disabled(client, monkeypatch):
    """检索未启用（disabled）时对话照常进行，只是上下文为空。"""
    captured: dict = {}
    _install_chat_stubs(
        monkeypatch,
        RetrievalResult(mode=MODE_DISABLED, reason="未配置可用的向量模型"),
        captured,
    )

    with client.stream("GET", "/chat", params={"message": "你好"}) as resp:
        frames = [line for line in resp.iter_lines() if line]

    frame = _find_frame(frames, "retrieval")
    assert frame["mode"] == MODE_DISABLED
    assert "未配置" in frame["reason"]
    # 有无检索都不影响正常收尾
    assert frames[-1] == "data: [DONE]"


def test_chat_fallback_path_reports_disabled(client, monkeypatch):
    """数据库不可用时回退内存路径，仍下发 disabled 帧，保证前端逻辑单一。"""
    def broken_session():
        raise RuntimeError("数据库不可用")

    captured: dict = {}
    monkeypatch.setattr(chat_module, "SessionLocal", broken_session)

    async def fake_stream(message, conversation_id=None, history=None, meta=None,
                           memory_enabled=True, context=None):
        captured["context"] = context
        yield "通用回答"

    monkeypatch.setattr(chat_module, "stream_answer", fake_stream)

    with client.stream("GET", "/chat", params={"message": "你好"}) as resp:
        frames = [line for line in resp.iter_lines() if line]

    frame = _find_frame(frames, "retrieval")
    assert frame["mode"] == MODE_DISABLED
    # 回退路径不注入检索上下文（context=None → 走通用链）
    assert captured.get("context") is None
    assert frames[-1] == "data: [DONE]"
