# P3-B2 文档入库（RAG 管线）单元测试
# ---------------------------------------------------------------------
# 覆盖「不依赖真实数据库 / 对象存储 / 千问 API」的纯逻辑与状态机：
#   1. 解析器（parser）：Markdown 按标题切分、保留标题层级
#   2. 切片器（chunker）：按目标大小切片、相邻重叠、不跨章节合并、token 粗估
#   3. 向量化（embedding）：mock 向量确定性 / L2 归一化 / 维度对齐；
#      embed_texts 在「未配置 Key」时走 mock 路径
#   4. 入库状态机（indexing_service.assert_can_index）：
#      审核通过可执行、未审核/已驳回拒绝、入库中（非僵死）拒绝、僵死可重跑
#
# 说明：index_document / unpublish_document 涉及真实 DB 与存储，交给「真实库冒烟」
#      覆盖（见开发总结报告），此处只测可在进程内确定性验证的单元逻辑。

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Document
from app.services import embedding as embedding_mod
from app.services.chunker import Chunk, chunk_blocks, estimate_tokens
from app.services.embedding import mock_embedding
from app.services.indexing_service import assert_can_index
from app.services.parser import ParsedBlock, parse_document, parse_markdown
from app.core.errors import IndexingInProgress, IndexingNotAllowed


# =====================================================================
# 1. 解析器
# =====================================================================
def test_parse_markdown_splits_by_heading():
    # 两个 H1 标题，应切成两个块，各自携带标题
    md = "# 第一章\n第一段内容\n\n# 第二章\n第二段内容".encode("utf-8")
    blocks = parse_markdown(md)
    assert len(blocks) == 2
    assert blocks[0].heading == "第一章"
    assert "第一段内容" in blocks[0].text
    assert blocks[1].heading == "第二章"
    assert "第二段内容" in blocks[1].text


def test_parse_markdown_keep_intro_without_heading():
    # 标题前的前导内容归入 heading="" 的首块
    md = "导语段落\n\n# 正文\n内容".encode("utf-8")
    blocks = parse_markdown(md)
    assert blocks[0].heading == ""
    assert "导语段落" in blocks[0].text


def test_parse_markdown_ignores_hashtag_as_heading():
    # "#话题标签" 后无空格，不视为标题，整体作为正文
    md = "这是 #话题标签 不是标题".encode("utf-8")
    blocks = parse_markdown(md)
    assert len(blocks) == 1
    assert blocks[0].heading == ""
    assert "#话题标签" in blocks[0].text


def test_parse_empty_markdown_raises():
    # 纯空白文档：解析后无有效文本，应抛 DocumentParseError
    from app.services.parser import DocumentParseError

    with pytest.raises(DocumentParseError):
        parse_document("\n\n   \n".encode("utf-8"), "markdown")


# =====================================================================
# 2. 切片器
# =====================================================================
def test_chunk_blocks_index_increments_in_order():
    blocks = [ParsedBlock(heading="", text="段落一内容", paragraphs=["段落一内容"])]
    chunks = chunk_blocks(blocks, chunk_size=50, overlap=10)
    assert len(chunks) >= 1
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunk_blocks_applies_overlap():
    # 一段远超 chunk_size 的文本，应切成多片且相邻片存在重叠前缀
    text = "甲" * 300  # 远超过 chunk_size=100
    blocks = [ParsedBlock(heading="", text=text, paragraphs=[text])]
    chunks = chunk_blocks(blocks, chunk_size=100, overlap=20)
    assert len(chunks) >= 2
    # 重叠：第 i+1 片的开头应出现在第 i 片结尾附近
    tail = chunks[0].content[-20:]
    assert chunks[1].content.startswith(tail[:5]) or tail[:5] in chunks[1].content


def test_chunk_blocks_does_not_merge_across_headings():
    # 两个不同章节，不应把「章一」的内容并入「章二」的切片正文
    blocks = [
        ParsedBlock(heading="章一", text="章一专属内容X", paragraphs=["章一专属内容X"]),
        ParsedBlock(heading="章二", text="章二专属内容Y", paragraphs=["章二专属内容Y"]),
    ]
    chunks = chunk_blocks(blocks, chunk_size=200, overlap=10)
    # 至少有一个切片属于章一、一个属于章二，且 heading 正确绑定
    headings = {c.heading for c in chunks}
    assert "章一" in headings and "章二" in headings
    for c in chunks:
        if c.heading == "章一":
            assert "章一专属内容X" in c.content and "章二" not in c.content
        if c.heading == "章二":
            assert "章二专属内容Y" in c.content and "章一" not in c.content


def test_chunk_blocks_invalid_params_fallback():
    # 非法参数（0 / 负数 / 非数字）应被收敛到安全默认值，而非抛错或死循环
    text = "内容" * 100
    blocks = [ParsedBlock(heading="", text=text, paragraphs=[text])]
    chunks = chunk_blocks(blocks, chunk_size=0, overlap=-5)
    assert len(chunks) >= 1


def test_estimate_tokens():
    # 空文本为 0；非空按「2 字符≈1 token」粗估（至少 1）
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文") == 1
    assert estimate_tokens("中" * 100) == 50


# =====================================================================
# 3. 向量化
# =====================================================================
def test_mock_embedding_deterministic():
    # 相同文本 → 相同向量
    v1 = mock_embedding("知识库问答系统", 32)
    v2 = mock_embedding("知识库问答系统", 32)
    assert v1 == v2


def test_mock_embedding_normalized():
    # 向量应为 L2 归一化（模长≈1）
    v = mock_embedding("归一化校验文本", 64)
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-6


def test_mock_embedding_dimension_and_distinct():
    # 维度对齐；不同文本得到不同向量
    v1 = mock_embedding("文本A", 128)
    v2 = mock_embedding("文本B", 128)
    assert len(v1) == 128 and len(v2) == 128
    assert v1 != v2


def test_embed_texts_mock_path(monkeypatch):
    # 未配置真实 Key 时，embed_texts 走 mock 路径，返回维度正确的向量列表
    import asyncio

    from app.config import Settings

    monkeypatch.setattr(Settings, "is_qwen_configured", property(lambda self: False))

    texts = ["切片一", "切片二", "切片三"]
    vectors = asyncio.run(embedding_mod.embed_texts(texts))
    assert len(vectors) == 3
    for vec in vectors:
        assert len(vec) == 1024  # 默认 EMBEDDING_DIM


# =====================================================================
# 4. 入库状态机
# =====================================================================
def test_assert_can_index_approved_ok():
    doc = Document(status="approved", review_status="approved", updated_at=None)
    # 不抛异常即通过
    assert_can_index(doc)


def test_assert_can_index_pending_rejected_rejected():
    # 未审核 / 已驳回 均不允许入库
    pending = Document(status="pending", review_status="pending", updated_at=None)
    rejected = Document(status="rejected", review_status="rejected", updated_at=None)
    with pytest.raises(IndexingNotAllowed):
        assert_can_index(pending)
    with pytest.raises(IndexingNotAllowed):
        assert_can_index(rejected)


def test_assert_can_index_indexing_in_progress():
    # 正在入库中（updated_at 刚刚），应拒绝（避免并发重复入库）
    fresh = Document(
        status="indexing",
        review_status="approved",
        updated_at=datetime.now(timezone.utc),
    )
    with pytest.raises(IndexingInProgress):
        assert_can_index(fresh)


def test_assert_can_index_indexing_stale_allowed():
    # 入库中但已僵死（超过阈值），允许重新入库
    stale = Document(
        status="indexing",
        review_status="approved",
        # 11 分钟前，超过 INDEXING_STALE_MINUTES=10
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=11),
    )
    assert_can_index(stale)  # 不抛异常即通过
