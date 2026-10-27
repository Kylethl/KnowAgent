# 文档切片器（P3-A1：切片策略）
# ---------------------------------------------------------------------
# 职责：把解析器产出的「结构化文本块」切成**适合检索与上下文拼装**的文本切片。
#
# 对应《需求清单》P3 功能2：AI Agent「文档解析与切片策略」。
#
# 切片原则：
#   1. 优先在**自然边界**断开：先按段落，段落过长再按句子，句子仍过长才硬切字符。
#      这样切片内语义完整，检索召回质量更高。
#   2. 相邻切片保留**重叠（overlap）**：把上一片末尾若干字符带到下一片开头，
#      缓解「答案恰好跨越切片边界」导致的召回失败。
#   3. **不跨章节合并**：不同 heading 的内容不拼进同一片，避免标题语义混淆。
#   4. heading 只写入 metadata，不重复进正文——正文保持纯净，
#      检索时由 P4 在拼装上下文时按需补标题。

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from ..core.logging import get_logger
from .parser import ParsedBlock

logger = get_logger(__name__)

# 句子切分正则：中英文句末标点 + 换行，均可作为安全的断句点
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")


@dataclass
class Chunk:
    """一个待向量化的文本切片。

    Attributes:
        index: 切片在整篇文档中的序号（从 0 开始，按原文顺序）。
        content: 切片正文（含与上一片的重叠前缀）。
        heading: 该片所属章节标题（无标题时为空字符串）。
        token_count: 粗估 token 数，用于上下文预算与统计。
    """

    index: int
    content: str
    heading: str
    token_count: int


def estimate_tokens(text: str) -> int:
    """粗估文本的 token 数（用于上下文预算与统计，非精确计费值）。

    中文约 1 字 1 token、英文约 4 字符 1 token，取折中的「2 字符 ≈ 1 token」，
    与 services/conversation_service.estimate_tokens 的口径不同（那里按英文本 4 计），
    因为知识库文档以中文为主，按 2 计更贴近实际。

    Args:
        text: 待估算文本。

    Returns:
        估算 token 数（至少 1，空文本为 0）。
    """
    if not text:
        return 0
    return max(1, len(text) // 2)


def _split_long_text(text: str, limit: int) -> List[str]:
    """把超长文本切到不超过 limit 的片段序列。

    切分顺序（由自然到强制）：句子 → 固定长度窗口。
    保证返回的每个片段长度 <= limit（除非单个「不可分割」字符序列本身超长）。

    Args:
        text: 待切分的长文本。
        limit: 单片段最大字符数。

    Returns:
        切分后的片段列表（保持原顺序）。
    """
    if len(text) <= limit:
        return [text]

    # 1) 先按句子切
    pieces: List[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        sentence = (sentence or "").strip()
        if not sentence:
            continue
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        # 2) 句子仍超长：按固定窗口硬切（无空格语言（如中文）只能这样兜底）
        start = 0
        while start < len(sentence):
            pieces.append(sentence[start : start + limit])
            start += limit
    return pieces


def chunk_blocks(
    blocks: List[ParsedBlock],
    chunk_size: int = 800,
    overlap: int = 120,
) -> List[Chunk]:
    """把结构化文本块切分为切片列表（切片主入口）。

    算法：
      对每个块（章节）：
        1. 把块内段落展开为「不大于 chunk_size 的片段」；
        2. 贪心累积片段形成切片，累积长度将超 chunk_size 时先落一片；
        3. 落片后把该片末尾 overlap 个字符作为下一片的开头（保持上下文连续）；
        4. 块结束或切换时强制落片，保证不跨章节合并。

    Args:
        blocks: 解析器产出的文本块（见 services/parser.py）。
        chunk_size: 目标切片字符数（>0）。
        overlap: 相邻切片重叠字符数（>=0；会被收敛到 chunk_size//2 以内，
                 避免重叠过大导致切片几乎不推进）。

    Returns:
        Chunk 列表（index 按顺序递增；输入为空时返回空列表）。
    """
    # 参数兜底：非法值收敛到安全区间，避免死循环或空切片。
    # 缺省/非法一律回退到默认值（而非 1），保证「配置填错」时仍产出可用切片。
    try:
        size = int(chunk_size)
    except (TypeError, ValueError):
        size = 800
    if size <= 0:
        size = 800
    try:
        ov = int(overlap)
    except (TypeError, ValueError):
        ov = 120
    if ov < 0:
        ov = 120
    # 重叠不得超过一半，否则切片会退化为「原地踏步」
    ov = min(ov, size // 2)

    chunks: List[Chunk] = []
    buf = ""  # 当前累积中的切片内容
    current_heading = ""

    def _flush(heading: str) -> None:
        """把累积内容落为一片，并把末尾 overlap 字符留给下一片。"""
        nonlocal buf
        content = buf.strip()
        if content:
            chunks.append(
                Chunk(
                    index=len(chunks),
                    content=content,
                    heading=heading,
                    token_count=estimate_tokens(content),
                )
            )
            # 保留末尾 overlap 个字符作为下一片的开头（上下文重叠）
            buf = content[-ov:] if ov > 0 else ""
        else:
            buf = ""

    for block in blocks:
        # 章节切换：先把上一章节的剩余内容落片，避免跨章节合并
        if buf.strip() and block.heading != current_heading:
            _flush(current_heading)
        current_heading = block.heading or ""

        # 1) 展开为不超过 size 的片段（块内优先用段落，段落过长再按句子/窗口切）
        units: List[str] = []
        paragraphs = block.paragraphs or ([block.text] if block.text else [])
        for paragraph in paragraphs:
            paragraph = (paragraph or "").strip()
            if not paragraph:
                continue
            units.extend(_split_long_text(paragraph, size))

        # 2) 贪心累积成片
        for unit in units:
            if not buf:
                buf = unit
                continue
            # 加上下一片会超出目标大小 → 先落片，再带上重叠继续
            if len(buf) + len(unit) + (1 if buf else 0) > size:
                _flush(current_heading)
                # 落片后 buf 已是重叠前缀；拼接时补一个换行分隔，避免首尾粘连
                buf = f"{buf}\n{unit}" if buf else unit
            else:
                buf = f"{buf}\n{unit}"

        # 3) 块结束：落片，保证不同章节不混在一片里
        if buf.strip():
            _flush(current_heading)
            buf = ""

    # 收尾（正常情况下上面已清空，这里防御性处理）
    if buf.strip():
        _flush(current_heading)

    logger.debug(
        "切片完成：%d 片（chunk_size=%d，overlap=%d）", len(chunks), size, ov
    )
    return chunks
