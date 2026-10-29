# 引用一致性守卫的单元测试（P4-B2 加固）
# ---------------------------------------------------------------------
# 这里守的是一条**不变量**：回答正文里出现的每个来源编号，都必须能在
# 本轮 citations 列表里找到对应项。它把「模型会不会乱标引用」这种概率性行为，
# 变成可以用固定输入反复验证的确定性断言——不必依赖"再调一次模型看它听不听话"。

import pytest

import app.services.citation_guard as guard


# =====================================================================
# 一、正文编号解析
# =====================================================================


def test_parse_plain_and_multi_indices():
    """单编号、连续多编号、逗号分隔都要能拆开并去重升序。"""
    assert guard.parse_citation_indices("支持 docx[1]") == [1]
    assert guard.parse_citation_indices("甲[1][3]乙") == [1, 3]
    assert guard.parse_citation_indices("甲[3,1]乙") == [1, 3]
    assert guard.parse_citation_indices("甲［2］乙") == [2]


def test_parse_ignores_markdown_links():
    """Markdown 链接 `[1](url)` 不是引用标注，不能被误判成幻觉引用。"""
    assert guard.parse_citation_indices("参见 [1](https://example.com/x)") == []


def test_parse_empty_answer():
    """空回答/无标注时返回空列表，不抛异常。"""
    assert guard.parse_citation_indices("") == []
    assert guard.parse_citation_indices(None) == []
    assert guard.parse_citation_indices("没有任何标注的普通回答") == []


# =====================================================================
# 二、一致性判定
# =====================================================================


def test_consistency_ok_when_every_index_is_cited():
    """正文编号都在引用列表里 → 一致。"""
    result = guard.check_citation_consistency("支持 docx[1]，也支持 md[2]", [{"index": 1}, {"index": 2}])

    assert result.ok is True
    assert result.dangling == []
    assert result.referenced == [1, 2]
    assert result.valid == [1, 2]


def test_consistency_flags_dangling_index():
    """正文标了引用列表里没有的编号 → 判为幻觉引用。"""
    result = guard.check_citation_consistency("支持 docx[1][9]", [{"index": 1}])

    assert result.ok is False
    assert result.dangling == [9]


def test_consistency_flags_citation_without_any_list():
    """回归用例（本次线上缺陷）：零命中时 citations 为空，正文却带 [1]。

    这正是「徽标显示知识库未命中、正文却标了来源」的矛盾场景，
    守卫生效后必须报出 dangling=[1]，指标也需要 +1。
    """
    result = guard.check_citation_consistency("基于知识库中已入库的文档回答问题[1]", [])

    assert result.ok is False
    assert result.dangling == [1]
    assert result.valid == []


def test_consistency_tolerates_dirty_citations():
    """引用列表里的脏数据（非 dict、缺 index、字符串编号）不能让校验崩掉。"""
    citations = [None, {"doc_id": "x"}, {"index": "2"}, {"index": 1}]  # type: ignore[list-item]

    result = guard.check_citation_consistency("答案[1][2]", citations)

    # 只有整数 index 被视为合法；"2" 这种字符串不认，因此 2 仍算悬空
    assert result.valid == [1]
    assert result.dangling == [2]


# =====================================================================
# 三、上报（日志 + 指标）
# =====================================================================


def test_guard_answer_increments_metric_on_dangling(monkeypatch):
    """发现悬空编号时累加指标，供 Grafana 告警。"""
    calls: list[int] = []
    monkeypatch.setattr(guard.metrics, "inc_citation_invalid", lambda: calls.append(1))

    result = guard.guard_answer("答案[7]", [])

    assert result.dangling == [7]
    assert len(calls) == 1


def test_guard_answer_does_not_increment_when_consistent(monkeypatch):
    """一致时不打点，避免指标长期恒增失去告警意义。"""
    calls: list[int] = []
    monkeypatch.setattr(guard.metrics, "inc_citation_invalid", lambda: calls.append(1))

    guard.guard_answer("答案[1]", [{"index": 1}])

    assert calls == []


# =====================================================================
# 四、端到端：假模型故意乱标引用
# =====================================================================


class _HallucinatingChunk:
    """模拟一个"很听话地标引用、但资料是空的"模型输出块。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.response_metadata = {"finish_reason": "stop"}


class _HallucinatingChain:
    """不管输入是什么，都吐出一段带 [1] 的回答——重现线上缺陷的模型行为。"""

    async def astream(self, inputs):
        for piece in ["知识库中未找到相关内容，", "但一般可以这样做[1]。"]:
            yield _HallucinatingChunk(piece)


class _LLMSettings:
    """生成链所需的配置桩（仅暴露 chat_chain 读取的字段）。"""

    SYSTEM_PROMPT = "你是知识库助手。"
    is_deepseek_configured = True


@pytest.mark.asyncio
async def test_hallucinated_citation_is_caught_end_to_end(monkeypatch):
    """零命中 + 模型乱标 [1] 时，守卫必须报出来（而非静默放过）。

    这是本次缺陷的端到端回归：路由层（改走通用链）负责"尽量不发生"，
    守卫层负责"发生了也一定能被发现"，两层合起来才闭环。
    """
    import app.services.chat_chain as chat_chain

    monkeypatch.setattr(chat_chain, "get_settings", lambda: _LLMSettings())
    monkeypatch.setattr(chat_chain, "get_chat_chain", lambda: _HallucinatingChain())

    parts = [
        p
        async for p in chat_chain.stream_answer(
            "你可以做什么", history=[], meta={}, memory_enabled=False, context=""
        )
    ]
    answer = "".join(parts)

    calls: list[int] = []
    monkeypatch.setattr(guard.metrics, "inc_citation_invalid", lambda: calls.append(1))
    result = guard.guard_answer(answer, [])

    assert "[1]" in answer
    assert result.dangling == [1]
    assert len(calls) == 1
