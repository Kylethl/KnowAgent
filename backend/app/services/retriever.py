# 知识库检索器（P4-A1：retriever + 可选重排）
# ---------------------------------------------------------------------
# 职责：把「用户提问」变成「可注入 Prompt 的参考资料」：
#         embed_query(提问) → pgvector 余弦相似度粗排召回 → 轻量重排 → 上下文拼装 → 引用映射
#
# 对应《需求清单》P4 功能1：
#   - 后端：/chat 改造为检索增强 —— 先 pgvector 相似度召回 → 拼装上下文 → DeepSeek 生成
#   - AI Agent：检索器（retriever）+ 可选重排（rerank）→ 生成链（LangChain）
#
# 设计要点：
#   1. **只检索「已入库」文档**：SQL 侧过滤 documents.status='indexed' 且未软删除，
#      保证未审核/已下线的文档绝不进入回答（这是审核流程的意义所在）。
#   2. **两级召回**：向量粗排取 RAG_CANDIDATE_K 条 → 重排取 RAG_TOP_K 条。
#      重排用「向量相似度 + 查询词覆盖率」的启发式打分（无第三方 rerank 服务、零额外费用），
#      接入专用 rerank 模型时只需替换 rerank() 的实现，调用方不变。
#   3. **降级优先于报错**：检索是「增强」而非「必需」，任何环节失败都不得让对话挂掉。
#      retrieve_for_chat() 内部吞掉全部异常并返回 mode 标记，由 API 层下发给前端展示。
#   4. **mode 语义**（前端据此渲染不同提示）：
#        rag       正常命中，回答基于知识库
#        no_hit    检索成功但无相关切片，回答为通用回答
#        disabled  检索未启用（配置关闭 / embedding 为 mock）
#        fallback  检索过程中出错（DB/向量接口异常），已回退为通用回答

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..core.logging import get_logger
from ..db.models import Document, DocumentChunk
from . import embedding as embedding_mod
from . import retrieval_cache

logger = get_logger(__name__)

# ---- 检索结果模式（与前端 RetrievalInfo.mode 严格一致） ----
MODE_RAG = "rag"
MODE_NO_HIT = "no_hit"
MODE_DISABLED = "disabled"
MODE_FALLBACK = "fallback"

# 重排打分权重：向量相似度为主，查询词覆盖率为辅。
# 之所以把关键词压到 0.25：中文 2-gram 的精确命中很稀疏、噪声大，
# 权重过高会出现「只是撞了几个词、语义并不相关」的切片把正确切片挤下去；
# 它的正确定位是「在语义接近时打破平局」。
_W_VECTOR = 0.75
_W_KEYWORD = 0.25
# 章节标题命中查询词时的加分（小幅度：标题相关说明切片更聚焦该主题）
_HEADING_BONUS = 0.05
# 引用片段的最大展示长度（字符）：引用卡片只需一段摘要，全文放前端无意义且浪费带宽
_SNIPPET_MAX_CHARS = 120


@dataclass
class RetrievedChunk:
    """一条命中的知识库切片（检索器对外的统一数据结构）。"""

    # 切片 ID / 所属文档 ID
    chunk_id: str
    doc_id: str
    # 文档标题（当前取原始文件名）与文档类型（word/markdown）
    doc_title: str
    file_type: str
    # 切片序号与正文
    chunk_index: int
    content: str
    # 所属章节标题（来自切片 metadata.heading，可能为空）
    heading: str
    # 最终排序得分（重排后写入；未重排时等于向量相似度）。
    # 这是「用于排序与展示」的综合分，不用于阈值过滤。
    score: float
    # 原始向量余弦相似度（0~1）：阈值过滤只认这个值，便于排查「为什么被过滤/为什么排第一」
    vector_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 化的字典（用于 Redis 缓存与 SSE 下发）。"""
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "file_type": self.file_type,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "heading": self.heading,
            "score": self.score,
            "vector_score": self.vector_score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetrievedChunk":
        """从缓存反序列化（字段缺失时给出安全默认值，避免旧缓存导致崩溃）。"""
        score = float(data.get("score", 0.0) or 0.0)
        return cls(
            chunk_id=str(data.get("chunk_id", "")),
            doc_id=str(data.get("doc_id", "")),
            doc_title=str(data.get("doc_title", "")),
            file_type=str(data.get("file_type", "")),
            chunk_index=int(data.get("chunk_index", 0) or 0),
            content=str(data.get("content", "")),
            heading=str(data.get("heading", "") or ""),
            score=score,
            # 兼容旧缓存（无 vector_score 字段）：退回综合分，避免展示 0
            vector_score=float(data.get("vector_score", score) or 0.0),
        )


@dataclass
class RetrievalResult:
    """一次检索的完整结果（既有数据也带「为什么是这个结果」的元信息）。

    Attributes:
        mode: 见模块头的四种模式之一。
        chunks: 最终注入 Prompt 的切片（已按相关度降序）。
        context: 拼装好的参考资料文本（直接塞进 Prompt；无命中为空串）。
        reason: 降级/未启用的原因（供日志与前端提示，正常命中为 None）。
        elapsed_ms: 检索耗时（毫秒），用于性能观测。
        cached: 是否命中了缓存。
    """

    mode: str
    chunks: List[RetrievedChunk] = field(default_factory=list)
    context: str = ""
    reason: Optional[str] = None
    elapsed_ms: int = 0
    cached: bool = False

    @property
    def hit_count(self) -> int:
        """命中条数（前端「命中 N 段资料」展示用）。"""
        return len(self.chunks)

    def meta_payload(self) -> Dict[str, Any]:
        """构造下发给前端的检索元信息帧内容（不含正文，控制帧体积）。

        Returns:
            形如 {"type": "retrieval", "mode": "rag", "count": 3, ...} 的字典。
            注意：引用正文（citations）属 P4 功能2 的契约，本功能只下发统计信息。
        """
        payload: Dict[str, Any] = {
            "type": "retrieval",
            "mode": self.mode,
            "count": self.hit_count,
            "cached": self.cached,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


# =====================================================================
# 查询词处理与重排
# =====================================================================

# 中文停用词/疑问词：这些词在所有文档里都可能出现，不具区分度，参与打分只会引入噪声
_STOPWORDS = {
    "什么", "怎么", "如何", "哪些", "哪个", "为何", "是否", "可以", "需要",
    "请问", "介绍", "一下", "这个", "那个", "我们", "你们", "以及", "还有",
    "the", "and", "for", "with", "how", "what", "why", "does", "is",
}


def extract_query_terms(query: str) -> Set[str]:
    """从查询语句中抽取用于计算覆盖率的「词项」。

    策略（无分词依赖，够用且稳定）：
      - 英文/数字：按非字母数字边界切词，长度 ≥2 才算有效词；
      - 中文：按 2-gram 滑窗切分（中文双字组合已能表达大部分语义单元），
        同时保留长度 ≥2 的停用词过滤，避免「什么/如何」这类高频词拉高覆盖率。

    Args:
        query: 用户提问。

    Returns:
        去重后的词项集合；查询无有效词项时返回空集合。
    """
    text = (query or "").strip().lower()
    if not text:
        return set()

    terms: Set[str] = set()
    # 提取英文/数字词（中文会被切分符隔开，这里只取纯 ASCII 词）
    ascii_buffer: List[str] = []

    def _flush_ascii() -> None:
        """把累积的 ASCII 字符组成一个词并入集合。"""
        if not ascii_buffer:
            return
        word = "".join(ascii_buffer)
        ascii_buffer.clear()
        if len(word) >= 2 and word not in _STOPWORDS:
            terms.add(word)

    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "-_"):
            ascii_buffer.append(ch)
            continue
        _flush_ascii()
        # 非 ASCII（中文/标点）暂不处理，中文在下面按字符整体做 2-gram
    _flush_ascii()

    # 中文 2-gram：只对连续的中文字符片段做滑窗，避免跨标点拼出无意义组合
    for segment in _split_cjk_segments(text):
        if len(segment) < 2:
            # 单字查询（如「钱」）保留单字，否则词项为空导致覆盖率恒为 0
            if segment:
                terms.add(segment)
            continue
        for i in range(len(segment) - 1):
            gram = segment[i : i + 2]
            if gram not in _STOPWORDS:
                terms.add(gram)

    return terms


def _split_cjk_segments(text: str) -> List[str]:
    """把文本切分为连续「中日韩字符」片段（用于中文 2-gram）。

    Args:
        text: 已转小写的查询文本。

    Returns:
        连续中文字符片段列表（标点/空格/英文处断开）。
    """
    segments: List[str] = []
    current: List[str] = []
    for ch in text:
        # 常用汉字 + 扩展 A 区的基本判断：用 Unicode 码点区间近似，
        # 不引入 jieba 等分词依赖（P4 只需粗粒度词项）
        if "\u4e00" <= ch <= "\u9fff":
            current.append(ch)
        else:
            if current:
                segments.append("".join(current))
                current = []
    if current:
        segments.append("".join(current))
    return segments


def keyword_coverage(terms: Iterable[str], text: str) -> float:
    """计算查询词项在给定文本中的覆盖率（0~1）。

    Args:
        terms: 查询词项集合（见 extract_query_terms）。
        text: 待匹配的文本（切片正文 + 标题）。

    Returns:
        命中词项数 / 总词项数；词项为空时返回 0.0。
    """
    term_list = [t for t in terms if t]
    if not term_list:
        return 0.0
    lowered = (text or "").lower()
    hit = sum(1 for term in term_list if term in lowered)
    return hit / len(term_list)


def rerank(
    query: str,
    chunks: List[RetrievedChunk],
    top_k: int,
    enabled: bool = True,
) -> List[RetrievedChunk]:
    """对粗排结果做重排，返回最终注入 Prompt 的 Top-K 切片。

    打分公式（可解释、无外部依赖）：
        final = 0.65 * 向量相似度 + 0.35 * 查询词覆盖率 + (标题命中 ? 0.05 : 0)
    其中覆盖率同时计入切片正文与章节标题，标题命中额外给一点加分。

    Args:
        query: 用户提问。
        chunks: 粗排（向量召回）得到的候选切片。
        top_k: 最终保留条数（≤0 时返回空列表）。
        enabled: 是否启用重排；False 时直接按向量分数截断（保留纯向量排序语义）。

    Returns:
        按最终得分降序的切片列表（score 字段被更新为**最终得分**，
        vector_score 保留原始余弦相似度，便于排查「排第一是因为语义还是因为关键词」）。
    """
    if top_k <= 0:
        return []
    if not chunks:
        return []

    # 关闭重排：完全按向量相似度排序后截断
    if not enabled:
        return sorted(chunks, key=lambda c: c.score, reverse=True)[:top_k]

    terms = extract_query_terms(query)
    scored: List[RetrievedChunk] = []
    for chunk in chunks:
        # 覆盖率在「正文 + 章节标题」上计算，标题命中再额外加分
        coverage = keyword_coverage(terms, f"{chunk.heading}\n{chunk.content}")
        heading_hit = bool(terms) and keyword_coverage(terms, chunk.heading) > 0
        # 注意：用 vector_score 参与计算——此时 score 仍是粗排相似度，
        # 但显式取 vector_score 可避免「重排被调用两次导致分数叠加」的隐患
        base_similarity = chunk.vector_score or chunk.score
        final_score = (
            _W_VECTOR * base_similarity
            + _W_KEYWORD * coverage
            + (_HEADING_BONUS if heading_hit else 0.0)
        )
        # 上限截断到 1.0：加分项可能让结果略超 1，展示成 >1 的相似度会让人误解
        chunk.score = round(min(final_score, 1.0), 4)
        scored.append(chunk)

    scored.sort(key=lambda c: c.score, reverse=True)
    return dedupe_chunks(scored)[:top_k]


def dedupe_chunks(chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
    """按正文内容去重（保留先出现、即得分更高的那一条）。

    为什么需要去重：
        知识库允许同一篇文档被重复上传（或同一文档重入库后仍保留其他副本），
        这些副本的切片正文完全一致，向量也一致，会同时命中并占满 Top-K 名额
        ——结果就是「参考资料只有 1 篇，却占了 5 个编号」，既浪费上下文预算，
        又让回答出现重复的引用编号。

    只比较「去掉全部空白后的正文」，不比较 doc_id：不同文档的相同段落同样应合并。
    为什么去掉全部空白而不是压缩成单空格：中文段落里换行/缩进的差异会被压缩成
    空格残留（"本节描述 部署步骤" ≠ "本节描述部署步骤"），导致去重失效。

    Args:
        chunks: 已按得分降序排好的切片列表。

    Returns:
        去重后的列表（顺序保持原相对次序）。
    """
    seen: Set[str] = set()
    unique: List[RetrievedChunk] = []
    for chunk in chunks:
        # 归一化：移除全部空白字符，避免同一段落在不同文档里因换行/空格差异被误判
        fingerprint = "".join(chunk.content.split())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(chunk)
    if len(unique) != len(chunks):
        logger.info("检索结果去重：%d 段 → %d 段（重复正文已合并）", len(chunks), len(unique))
    return unique


# =====================================================================
# 数据库检索（pgvector 相似度召回）
# =====================================================================

async def search_chunks(
    db: AsyncSession,
    query_vector: List[float],
    limit: int,
    threshold: float,
) -> List[RetrievedChunk]:
    """按向量余弦相似度从 pgvector 召回候选切片。

    SQL 语义（SQLAlchemy 表达式）：
        SELECT chunk.*, doc.filename, 1 - (chunk.embedding <=> :vec) AS score
        FROM document_chunks JOIN documents
        WHERE documents.status = 'indexed' AND documents.deleted_at IS NULL
              AND chunk.embedding IS NOT NULL
        ORDER BY chunk.embedding <=> :vec   -- 距离升序 = 相似度降序
        LIMIT :limit

    说明：
      - `<=>` 是 pgvector 的余弦距离算子，相似度 = 1 - 距离；
      - 排序交给 pgvector（走 HNSW 索引 idx_document_chunks_embedding），
        阈值过滤在应用层做，避免「距离阈值 + 索引」在某些执行计划下退化为全表扫描。

    Args:
        db: 异步数据库会话。
        query_vector: 提问的向量（维度须等于 EMBEDDING_DIM）。
        limit: 召回条数上限（粗排候选数）。
        threshold: 相似度下限，低于该值的切片被丢弃。

    Returns:
        命中的切片列表（按相似度降序）。
    """
    if limit <= 0 or not query_vector:
        return []

    # 余弦距离表达式：值越小越相似
    distance = DocumentChunk.embedding.cosine_distance(query_vector)

    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.doc_id,
            DocumentChunk.chunk_index,
            DocumentChunk.content,
            DocumentChunk.metadata_json,
            Document.filename,
            Document.file_type,
            distance.label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.doc_id)
        .where(
            # 只有「已入库」且未软删除的文档参与检索（审核/下线即刻生效）
            Document.status == "indexed",
            Document.deleted_at.is_(None),
            # 历史遗留数据可能没有向量，排除以免 distance 为 NULL
            DocumentChunk.embedding.isnot(None),
        )
        .order_by(distance)
        .limit(limit)
    )

    rows = (await db.execute(stmt)).all()

    results: List[RetrievedChunk] = []
    for row in rows:
        # row 为具名元组；distance 可能因浮点误差略小于 0，做上下界收敛
        raw_distance = float(row.distance) if row.distance is not None else 1.0
        similarity = max(0.0, min(1.0, 1.0 - raw_distance))
        if similarity < threshold:
            # 低于阈值：判为不相关，宁可少给也不污染上下文
            continue

        meta = row.metadata_json or {}
        results.append(
            RetrievedChunk(
                chunk_id=str(row.id),
                doc_id=str(row.doc_id),
                # 标题优先取 metadata.filename（入库时快照），回退当前文件名
                doc_title=str(meta.get("filename") or row.filename or "未命名文档"),
                file_type=str(meta.get("file_type") or row.file_type or ""),
                chunk_index=int(row.chunk_index or 0),
                content=row.content or "",
                heading=str(meta.get("heading") or ""),
                # 粗排阶段：综合分先等于向量相似度（重排会覆盖 score，但保留 vector_score）
                score=round(similarity, 4),
                vector_score=round(similarity, 4),
            )
        )
    return results


# =====================================================================
# 上下文拼装与引用映射
# =====================================================================

def build_context(chunks: List[RetrievedChunk], max_chars: int) -> str:
    """把切片拼装成带编号的参考资料文本（编号即回答中的引用角标）。

    格式（编号从 1 开始，与 Prompt 中「用 [编号] 标注来源」的约束一一对应）：
        [1] 来源：《产品说明.md》 · 章节：常见问题
        问：支持哪些格式？答：目前支持 .docx 与 .md。旧版 .doc 需要转换。

        [2] 来源：《部署手册.md》
        ...

    Args:
        chunks: 已重排的切片列表（顺序即编号顺序）。
        max_chars: 总字符预算；超出预算的切片整条丢弃（不截半条，避免句子被切断误导模型）。

    Returns:
        拼装好的上下文文本；无切片或预算为 0 时返回空串。
    """
    if not chunks or max_chars <= 0:
        return ""

    blocks: List[str] = []
    used = 0
    for idx, chunk in enumerate(chunks, start=1):
        # 头部行：来源文档 + 章节（章节为空时不输出该段）
        header = f"[{idx}] 来源：《{chunk.doc_title}》"
        if chunk.heading:
            header += f" · 章节：{chunk.heading}"
        block = f"{header}\n{chunk.content.strip()}"

        # 预算判定：至少放入首条（否则检索等于白做），后续按预算截断
        if blocks and used + len(block) > max_chars:
            logger.info(
                "参考资料超出上下文预算（%d 字），第 %d 条起不再注入", max_chars, idx
            )
            break
        blocks.append(block)
        used += len(block) + 2  # +2 为块间空行

    return "\n\n".join(blocks)


def build_citations(chunks: List[RetrievedChunk]) -> List[Dict[str, Any]]:
    """把切片映射为「引用来源」列表（P4 功能2 的契约数据结构）。

    Args:
        chunks: 已重排的切片列表。

    Returns:
        引用列表，每项含序号、文档 ID/标题、切片序号、片段摘要、相关度分数与章节。
        `cited` 初始为 False，由 select_citations() 根据回答中的标注置为 True。
    """
    citations: List[Dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, start=1):
        # 摘要：压缩空白后按长度截断，超长补省略号
        snippet = " ".join(chunk.content.split())
        if len(snippet) > _SNIPPET_MAX_CHARS:
            snippet = snippet[:_SNIPPET_MAX_CHARS] + "…"
        citations.append(
            {
                # 与 Prompt 中 [编号] 对齐，前端可直接按序号做锚点跳转
                "index": idx,
                "doc_id": chunk.doc_id,
                "doc_title": chunk.doc_title,
                "file_type": chunk.file_type,
                "chunk_id": chunk.chunk_id,
                "chunk_index": chunk.chunk_index,
                "heading": chunk.heading,
                "snippet": snippet,
                # score 为综合排序分；vector_score 为语义相似度（前端「相关度」用它展示更直观）
                "score": chunk.score,
                "vector_score": chunk.vector_score,
                # 是否被本轮回答真正引用（由 select_citations 判定）
                "cited": False,
            }
        )
    return citations


# 回答中的引用标注：[1] / ［1］ / [1][3] / [1,3]（含中文逗号）都要能识别。
# 说明：模型偶尔会用全角方括号，这里一并兼容，否则会漏判「已引用」。
_CITATION_PATTERN = re.compile(r"[\[［]\s*(\d{1,3}(?:\s*[,，]\s*\d{1,3})*)\s*[\]］]")


def extract_cited_indices(answer: str, max_index: int) -> List[int]:
    """从回答正文中解析出被引用的资料编号（去重、升序）。

    用途：把「模型实际引用了哪几段资料」映射回检索结果，
    前端只展示真正被用到的引用卡片，避免把无关召回项当成来源展示。

    Args:
        answer: 助手完整回答文本。
        max_index: 本轮资料编号上限（防止模型幻觉出 [9] 这类越界编号）。

    Returns:
        去重升序的编号列表；未标注或全部越界时返回空列表。
    """
    if not answer or max_index <= 0:
        return []
    found: Set[int] = set()
    for match in _CITATION_PATTERN.finditer(answer):
        # 组内可能是 "1,3" 这样的多编号，逐个拆分
        for piece in re.split(r"[,，]", match.group(1)):
            piece = piece.strip()
            if not piece.isdigit():
                continue
            num = int(piece)
            # 越界编号直接丢弃：它无法对应到任何真实资料
            if 1 <= num <= max_index:
                found.add(num)
    return sorted(found)


def select_citations(
    chunks: List[RetrievedChunk], answer: str
) -> List[Dict[str, Any]]:
    """生成本轮最终下发的引用列表（P4-B2 的核心输出）。

    策略：
      - 回答里有有效标注 → 只保留被引用的资料，并置 `cited=True`
        （宁少不多：用户点开卡片时期望看到的就是「这句话的出处」）；
      - 回答里没有标注（模型没按 Prompt 标注）→ 回退为**全部检索资料**，
        `cited=False`，保证「答案是基于哪些资料生成的」依然可见，不至于出现
        「答案明显来自知识库、却没有任何来源」这种更糟的体验。

    Args:
        chunks: 本轮注入 Prompt 的切片（已重排）。
        answer: 助手完整回答文本。

    Returns:
        引用列表（按序号升序）；无切片时返回空列表。
    """
    if not chunks:
        return []

    all_citations = build_citations(chunks)
    used = set(extract_cited_indices(answer, len(all_citations)))
    if not used:
        # 未标注：全部作为「参考资料」下发，但明确标记未直接引用
        logger.info("回答未标注引用编号，回退为下发全部 %d 段资料", len(all_citations))
        return all_citations

    selected: List[Dict[str, Any]] = []
    for item in all_citations:
        if item["index"] in used:
            item["cited"] = True
            selected.append(item)
    logger.info(
        "引用映射：检索 %d 段，回答标注 %d 段（编号 %s）",
        len(all_citations), len(selected), ",".join(str(i) for i in sorted(used)),
    )
    return selected


# =====================================================================
# 对外主入口
# =====================================================================

def _cache_key(query: str, top_k: int, threshold: float) -> str:
    """构造缓存键（含 top_k 与阈值，保证参数变化时不会误用旧结果）。"""
    return retrieval_cache.build_cache_key(query, top_k, threshold)


async def retrieve_chunks(
    query: str,
    db: AsyncSession,
    query_vector: Optional[List[float]] = None,
) -> RetrievalResult:
    """执行一次完整检索（缓存 → 向量化 → 召回 → 重排 → 上下文）。

    本函数**会抛出异常**（DB/向量接口失败），供需要感知错误的调用方使用；
    对话链路请使用 retrieve_for_chat()（内部吞异常并降级）。

    Args:
        query: 用户提问。
        db: 异步数据库会话。
        query_vector: 可选的预计算向量（测试注入用）；为空时内部调用 embed_query。

    Returns:
        RetrievalResult（mode 为 rag 或 no_hit）。

    Raises:
        Exception: 向量化 / 数据库查询失败时向上抛出（由调用方决定如何降级）。
    """
    settings = get_settings()
    started = time.perf_counter()

    # 1) 缓存优先：命中则直接复用召回结果（省一次 embedding 调用 + 一次向量检索）
    cache = retrieval_cache.get_retrieval_cache()
    ck = _cache_key(query, settings.RAG_TOP_K, settings.RAG_SIMILARITY_THRESHOLD)
    cached_items = await cache.get(ck)
    if cached_items is not None:
        chunks = [RetrievedChunk.from_dict(item) for item in cached_items]
        context = build_context(chunks, settings.RAG_MAX_CONTEXT_CHARS)
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info("检索命中缓存：命中 %d 段，耗时 %dms", len(chunks), elapsed)
        return RetrievalResult(
            mode=MODE_RAG if chunks else MODE_NO_HIT,
            chunks=chunks,
            context=context,
            # 缓存为空列表说明上次确实无命中，如实返回 no_hit
            reason=None if chunks else "知识库中未找到相关内容",
            elapsed_ms=elapsed,
            cached=True,
        )

    # 2) 向量化：真实接口调用（mock 模式下由 embed_texts 内部处理）
    vector = query_vector if query_vector is not None else await embedding_mod.embed_query(query)

    # 3) 粗排召回：多召回一些候选供重排挑选
    candidates = await search_chunks(
        db,
        query_vector=vector,
        limit=max(settings.RAG_CANDIDATE_K, settings.RAG_TOP_K),
        threshold=settings.RAG_SIMILARITY_THRESHOLD,
    )

    # 4) 重排：向量相似度 + 查询词覆盖率，取最终 Top-K
    chunks = rerank(
        query,
        candidates,
        top_k=settings.RAG_TOP_K,
        enabled=settings.RAG_RERANK_ENABLED,
    )

    # 5) 上下文拼装（受字符预算约束）
    context = build_context(chunks, settings.RAG_MAX_CONTEXT_CHARS)

    elapsed = int((time.perf_counter() - started) * 1000)
    logger.info(
        "检索完成：候选 %d 段 → 采用 %d 段，mode=%s，耗时 %dms",
        len(candidates), len(chunks), MODE_RAG if chunks else MODE_NO_HIT, elapsed,
    )

    # 6) 回填缓存：空结果也缓存（负缓存），避免热门但无答案的问题反复打向量接口
    await cache.set(ck, [c.to_dict() for c in chunks])

    return RetrievalResult(
        mode=MODE_RAG if chunks else MODE_NO_HIT,
        chunks=chunks,
        context=context,
        reason=None if chunks else "知识库中未找到相关内容",
        elapsed_ms=elapsed,
        cached=False,
    )


async def retrieve_for_chat(query: str, db: Optional[AsyncSession]) -> RetrievalResult:
    """对话链路的检索入口：**保证不抛异常**，任何问题都降级为可用结果。

    降级顺序（对应前端展示的不同提示）：
      1. 配置层面不可用（RAG 关闭 / embedding 为 mock）→ disabled；
      2. 无数据库会话（本地无库回退路径）→ disabled（reason=database_unavailable）；
      3. 检索过程异常（DB/向量接口）→ fallback，回答退回通用模式；
      4. 正常但无命中 → no_hit，由 Prompt 引导模型如实说明「知识库未找到」。

    Args:
        query: 用户提问。
        db: 数据库会话；为 None 表示当前链路没有可用数据库。

    Returns:
        RetrievalResult（永不抛异常）。
    """
    settings = get_settings()

    # 1) 配置层面不可用：直接返回未启用，避免无意义地打向量接口
    if not settings.is_rag_ready:
        reason = "知识库检索已关闭" if not settings.RAG_ENABLED else "未配置可用的向量模型"
        logger.info("跳过知识库检索：%s", reason)
        return RetrievalResult(mode=MODE_DISABLED, reason=reason)

    # 2) 无数据库：无法查询向量表（P1 内存回退路径）
    if db is None:
        return RetrievalResult(mode=MODE_DISABLED, reason="数据库不可用，未执行知识库检索")

    # 3) 正常检索，异常统一降级
    try:
        return await retrieve_chunks(query, db)
    except Exception as exc:  # noqa: BLE001 —— 检索是增强项，任何异常都不能影响对话
        logger.warning("知识库检索失败，已降级为通用回答：%s", exc)
        return RetrievalResult(mode=MODE_FALLBACK, reason=f"检索失败：{exc}")


def reset_cache() -> None:
    """重置检索缓存单例（仅供测试使用）。

    说明：缓存实例内部持有 Redis 连接与内存字典，
    单测之间需要隔离时调用本函数即可获得干净状态。
    """
    retrieval_cache.reset_retrieval_cache()
