# 文档解析器（P3-A1：解析策略）
# ---------------------------------------------------------------------
# 职责：把「原始文件字节」解析为**带标题层级的结构化文本块**，供切片器消费。
#
# 对应《需求清单》P3 功能2：
#   - AI Agent：文档解析与切片策略（Word 用 python-docx、Markdown 直接解析）
#
# 设计要点：
#   1. 统一输出 ParsedBlock（heading + text）：
#      - Markdown 按 `#` 标题切分为多个 section；
#      - Word（.docx）按段落样式（Heading 1/2…）识别标题，表格转成文本行；
#      - 旧版 .doc（二进制格式）python-docx 不支持，直接抛错由上层给出明确提示。
#   2. 保留 heading 的意义：切片时按 heading 分组，避免把「第一章」和「第二章」
#      的内容混进同一切片；heading 也会写入切片 metadata，供 P4 引用卡片展示来源章节。
#   3. 本模块只做解析，不做切片（切片见 services/chunker.py），职责单一便于单测。

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import List

from ..core.logging import get_logger

logger = get_logger(__name__)

# Word 段落样式名前缀：docx 中标题段落的样式名为 "Heading 1" / "标题 1" 等。
# 同时兼容中文版 Word 的样式名（"标题 1"），避免中文文档识别不出标题。
_HEADING_STYLE_PREFIXES = ("heading", "标题")


class DocumentParseError(Exception):
    """文档解析失败（文件损坏 / 格式不支持 / 内容为空）。

    上层（services/indexing_service.py）捕获后写入 documents.last_index_error，
    并把文档状态回退，保证入库失败可观测、可重试。
    """


@dataclass
class ParsedBlock:
    """一个结构化文本块（通常对应文档的一个章节）。

    Attributes:
        heading: 该块所属的最近一级标题；无标题时为空字符串。
        text: 该块的正文（已去掉标题行本身）。
    """

    heading: str = ""
    text: str = ""
    # 块内段落列表（切片时按段落粒度再切，避免破坏原文换行结构）
    paragraphs: List[str] = field(default_factory=list)


def _is_heading_style(style_name: str | None) -> bool:
    """判断 Word 段落样式是否为标题样式。

    Args:
        style_name: 段落样式名（可能为 None）。

    Returns:
        是标题样式返回 True。
    """
    if not style_name:
        return False
    lowered = style_name.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _HEADING_STYLE_PREFIXES)


def parse_markdown(content: bytes) -> List[ParsedBlock]:
    """解析 Markdown 文本：按 ATX 标题（#/##/###）切分为多个文本块。

    标题行本身不进入正文，而是作为该块的 heading；
    没有标题的前置内容归入 heading="" 的首个块。

    Args:
        content: Markdown 文件字节。

    Returns:
        ParsedBlock 列表（按原文顺序）。
    """
    # Markdown 本身就是纯文本；errors='replace' 避免个别非法字节导致整体失败
    text = content.decode("utf-8", errors="replace")
    blocks: List[ParsedBlock] = []
    current = ParsedBlock(heading="", text="", paragraphs=[])

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        # ATX 标题：# / ## / ### …（最多 6 级），且必须紧跟空格才是标题（避免误判 `#话题标签`）
        is_heading = (
            stripped.startswith("#")
            and len(stripped) > 1
            and stripped.lstrip("#").startswith(" ")
        )
        if is_heading:
            # 先把上一个块收尾（若非空）
            if current.paragraphs:
                current.text = "\n".join(current.paragraphs)
                blocks.append(current)
            # 开启新块：去掉前导 # 与空格作为标题
            current = ParsedBlock(heading=stripped.lstrip("#").strip(), text="", paragraphs=[])
            continue

        # 普通行：空行用于分隔段落，这里把非空行作为段落保存（保留 Markdown 原样）
        if stripped:
            current.paragraphs.append(stripped)

    # 收尾最后一个块
    if current.paragraphs:
        current.text = "\n".join(current.paragraphs)
        blocks.append(current)

    logger.debug("Markdown 解析完成：%d 个文本块", len(blocks))
    return blocks


def parse_docx(content: bytes) -> List[ParsedBlock]:
    """解析 Word（.docx）：按段落样式识别标题，表格转换为文本行。

    说明：旧版 .doc（二进制格式）不是 zip 包，python-docx 无法解析，
          此时会抛出异常并由调用方转为 DocumentParseError。

    Args:
        content: .docx 文件字节。

    Returns:
        ParsedBlock 列表（按原文顺序）。

    Raises:
        DocumentParseError: 文件不是合法 docx（可能是 .doc 或已损坏）。
    """
    try:
        # 懒导入：仅解析 Word 时需要 python-docx
        from docx import Document as DocxDocument
    except ImportError as exc:  # pragma: no cover —— 依赖已在 requirements 中声明
        raise DocumentParseError("未安装 python-docx，无法解析 Word 文档") from exc

    try:
        docx = DocxDocument(BytesIO(content))
    except Exception as exc:  # noqa: BLE001 —— 损坏文件 / 旧版 .doc 都会在此失败
        raise DocumentParseError(
            "Word 文档解析失败（旧版 .doc 二进制格式或损坏文件，请转为 .docx 后重试）"
        ) from exc

    blocks: List[ParsedBlock] = []
    current = ParsedBlock(heading="", text="", paragraphs=[])

    def _flush() -> None:
        """把当前累积的块收尾并入结果（非空才入）。"""
        nonlocal current
        if current.paragraphs:
            current.text = "\n".join(current.paragraphs)
            blocks.append(current)
        current = ParsedBlock(heading=current.heading, text="", paragraphs=[])

    # 1) 段落：标题样式开启新块，普通样式作为正文段落
    for para in docx.paragraphs:
        para_text = para.text.strip()
        if not para_text:
            continue
        style_name = para.style.name if para.style is not None else ""
        if _is_heading_style(style_name):
            _flush()
            # 新块以该标题命名；标题文本不重复进正文（切片时由 metadata 携带）
            current = ParsedBlock(heading=para_text, text="", paragraphs=[])
        else:
            current.paragraphs.append(para_text)

    # 2) 表格：转为「单元格用 | 连接」的文本行，附到当前块（保留表格中的知识）
    for table in docx.tables:
        rows: List[str] = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            # 过滤整行全空的情况，避免产生无意义空行
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            current.paragraphs.extend(rows)

    _flush()
    logger.debug("Word 解析完成：%d 个文本块", len(blocks))
    return blocks


def parse_document(content: bytes, file_type: str) -> List[ParsedBlock]:
    """按文档类型解析为结构化文本块（解析入口）。

    Args:
        content: 原始文件字节。
        file_type: 文档类型（'markdown' / 'word'；P5 扩展 image/audio/video）。

    Returns:
        ParsedBlock 列表。

    Raises:
        DocumentParseError: 类型不支持、文件损坏或解析后内容为空。
    """
    if file_type == "markdown":
        blocks = parse_markdown(content)
    elif file_type == "word":
        blocks = parse_docx(content)
    else:
        raise DocumentParseError(f"暂不支持解析该类型文档：{file_type}")

    # 空文档（无有效文本）视为解析失败：避免产生 0 切片却标记为「已入库」的假成功
    if not blocks or not any(block.text.strip() for block in blocks):
        raise DocumentParseError("文档解析后无有效文本内容，无法入库")

    return blocks
