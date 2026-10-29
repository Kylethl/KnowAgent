# 引用一致性守卫（P4-B2 加固：输出契约校验）
# ---------------------------------------------------------------------
# 职责：回答生成之后，校验「正文里出现的每一个来源编号」都能在
#       citations 列表里找到对应项；找不到的就是模型编出来的（幻觉引用）。
#
# 为什么需要这一层：
#   Prompt 只能"尽力约束"模型，约束是概率性的。历史缺陷就是典型例子——
#   检索零命中时模型仍给通用知识标上 [1]，而 select_citations() 因为
#   chunks 为空返回空列表，前端也不渲染来源卡片，最终呈现为
#   「徽标说未命中、正文却带 [1]」的自相矛盾。
#   把这类矛盾变成一条**可断言的不变量**（正文编号 ⊆ 引用列表编号），
#   既能上线监控（指标 + 告警），也能被单测用固定输入反复验证，
#   不必依赖"再调一次模型看它这次听不听话"。
#
# 设计边界（重要）：
#   - 本模块只做**校验与上报**，不改写模型输出。
#     流式场景下 token 早已推给前端，事后剥离只能改落库内容、救不了当次页面，
#     真正有效的修法是生成前分流（见 chat_chain.stream_answer 的三路分支）。
#   - 判定基于文本正则，属于"结构校验"而非"事实校验"：
#     它能抓「编号不存在」，抓不到「编号存在但张冠李戴」。

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from ..core import metrics
from ..core.logging import get_logger

logger = get_logger(__name__)

# 回答中的来源标注：[1] / ［1］ / [1][3] / [1,3]（含中文逗号）都要识别。
#   - 全角方括号：模型偶尔混用，不兼容会漏判「已引用」；
#   - 结尾的 (?!\() 排除 Markdown 链接语法 `[1](https://...)`：
#     链接文字恰好是数字时不该被当成引用标注，否则会误报幻觉引用。
CITATION_PATTERN = re.compile(r"[\[［]\s*(\d{1,3}(?:\s*[,，]\s*\d{1,3})*)\s*[\]］](?!\()")


def parse_citation_indices(answer: Optional[str]) -> List[int]:
    """解析正文中出现的全部来源编号（不做有效性过滤）。

    与 retriever.extract_cited_indices 的区别：
        后者用 max_index 做上界过滤，只返回"能对上资料"的编号，用于挑选引用卡片；
        本函数保留**原样解析结果**，正是为了让调用方看出"哪些编号对不上"。

    Args:
        answer: 助手完整回答文本。

    Returns:
        去重升序的编号列表；无标注时返回空列表。
    """
    if not answer:
        return []
    found: set[int] = set()
    for match in CITATION_PATTERN.finditer(answer):
        # 组内可能是 "1,3" 这样的多编号，逐个拆分
        for piece in re.split(r"[,，]", match.group(1)):
            piece = piece.strip()
            if piece.isdigit():
                found.add(int(piece))
    return sorted(found)


def _collect_valid_indices(citations: Optional[Sequence[Dict[str, Any]]]) -> List[int]:
    """从 citations 列表里取出所有合法编号（升序去重）。

    容忍两种脏数据：条目不是 dict、以及缺少 index 字段——
    引用列表来自检索侧，不该让一处脏数据把整个校验打断。

    Args:
        citations: 引用列表，元素形如 {"index": 1, "doc_id": ..., "snippet": ...}。

    Returns:
        合法编号列表。
    """
    valid: set[int] = set()
    for item in citations or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        # bool 是 int 的子类，需要排除，避免 True 被当成 1
        if isinstance(index, int) and not isinstance(index, bool):
            valid.add(index)
    return sorted(valid)


@dataclass(frozen=True)
class CitationConsistency:
    """一次引用一致性校验的结果。

    Attributes:
        referenced: 正文里出现的全部编号（原样，含越界/幻觉编号）。
        valid: citations 里真实存在的编号。
        dangling: 正文有、引用列表没有的编号 —— 即幻觉引用。
    """

    referenced: List[int]
    valid: List[int]
    dangling: List[int]

    @property
    def ok(self) -> bool:
        """是否一致：没有任何无法映射的编号。"""
        return not self.dangling


def check_citation_consistency(
    answer: Optional[str], citations: Optional[Sequence[Dict[str, Any]]]
) -> CitationConsistency:
    """校验「正文编号 ⊆ 引用列表编号」这条不变量。

    Args:
        answer: 助手完整回答文本。
        citations: 本轮下发的引用列表（可为 None / 空列表）。

    Returns:
        CitationConsistency：包含正文编号、有效编号与对不上的编号。

    Examples:
        >>> check_citation_consistency("支持 docx[1]", [{"index": 1}]).ok
        True
        >>> check_citation_consistency("支持 docx[1]", []).dangling
        [1]
    """
    referenced = parse_citation_indices(answer)
    valid = _collect_valid_indices(citations)
    # 集合差：正文标了、但引用列表里没有 → 模型编的
    dangling = [num for num in referenced if num not in set(valid)]
    return CitationConsistency(referenced=referenced, valid=valid, dangling=dangling)


def guard_answer(
    answer: Optional[str], citations: Optional[Sequence[Dict[str, Any]]]
) -> CitationConsistency:
    """校验并上报：不一致时打告警日志 + 累加 Prometheus 指标。

    chat 接口在生成完回答、算完 citations 之后调用一次；
    指标 knowagent_citation_invalid_total 上升即说明模型在"编引用"，
    Grafana 上可以直接配告警，不必等用户截图反馈。

    Args:
        answer: 助手完整回答文本。
        citations: 本轮下发的引用列表。

    Returns:
        同 check_citation_consistency，供调用方（测试/日志）进一步使用。
    """
    result = check_citation_consistency(answer, citations)
    if result.dangling:
        metrics.inc_citation_invalid()
        logger.warning(
            "引用一致性告警：正文出现无法映射的来源编号 %s（本轮有效编号 %s）",
            result.dangling,
            result.valid,
        )
    return result
