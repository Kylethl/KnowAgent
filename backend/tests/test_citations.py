# P4 功能2「答案引用来源」测试
# ---------------------------------------------------------------------
# 覆盖四层：
#   1. 引用标注解析（[1] / ［1］ / [1,3] / 越界编号 / 去重）；
#   2. 引用筛选（只留被引用段；无标注时回退全部）；
#   3. /chat 接口契约（citations 帧在正文之后、[DONE] 之前；引用随消息落库）；
#   4. 引用跳转接口（已入库校验 / 切片归属校验 / 相邻片段导航）与历史回显。
#
# 原则（见《测试任务清单》）：不依赖真实 PostgreSQL / 大模型 / 网络，
# DB 用可控替身，LLM 与检索一律 stub。

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_module
from app.core.security import DEMO_USER_ID, get_current_user_id, get_current_user_id_sse
from app.db.engine import get_db
from app.main import create_app
from app.services.retriever import (
    RetrievedChunk,
    build_citations,
    extract_cited_indices,
    select_citations,
)


# =====================================================================
# 公共工具
# =====================================================================


def make_chunk(content: str, score: float = 0.5, heading: str = "", doc_title: str = "文档A") -> RetrievedChunk:
    """构造测试用切片。"""
    return RetrievedChunk(
        chunk_id=str(uuid4()),
        doc_id=str(uuid4()),
        doc_title=doc_title,
        file_type="markdown",
        chunk_index=0,
        content=content,
        heading=heading,
        score=score,
        vector_score=score,
    )


# =====================================================================
# 一、引用标注解析
# =====================================================================


def test_extract_simple_bracket():
    """最基本的 [1] 标注。"""
    assert extract_cited_indices("支持 docx 与 md[1]。", 3) == [1]


def test_extract_multiple_and_dedup():
    """多个标注、重复标注应去重并升序。"""
    assert extract_cited_indices("甲[2]，乙[1]，丙[2]再引[1]", 3) == [1, 2]


def test_extract_fullwidth_brackets():
    """模型偶尔输出全角方括号（中文输入法习惯），必须同样识别。"""
    assert extract_cited_indices("答案正文［2］", 3) == [2]


def test_extract_comma_list_inside_brackets():
    """[1,3] 这类合并写法要展开为两个编号（含中文逗号）。"""
    assert extract_cited_indices("同时参考[1,3]与[2，3]", 5) == [1, 2, 3]


def test_extract_drops_out_of_range():
    """越界编号无法对应任何真实资料，直接丢弃（防模型幻觉 [9]）。"""
    assert extract_cited_indices("引用了[1]和不存在[9]", 2) == [1]


def test_extract_returns_empty_without_markers():
    """无标注 / 空文本 / max_index 为 0 时返回空列表。"""
    assert extract_cited_indices("这是一段没有编号的回答", 3) == []
    assert extract_cited_indices("", 3) == []
    assert extract_cited_indices("有标注[1]", 0) == []


def test_extract_ignores_non_numeric_brackets():
    """方括号里不是数字（如 [注] / [a]）时不应被当成引用。"""
    assert extract_cited_indices("见[注]与[a]", 3) == []


# =====================================================================
# 二、引用筛选（select_citations）
# =====================================================================


def test_select_keeps_only_cited_chunks():
    """回答只标注了 [1]，则只下发第 1 段，其余不外泄为「来源」。"""
    chunks = [make_chunk("第一段", doc_title="A.md"), make_chunk("第二段", doc_title="B.md")]

    citations = select_citations(chunks, "结论来自第一段[1]。")

    assert len(citations) == 1
    assert citations[0]["index"] == 1
    assert citations[0]["doc_title"] == "A.md"
    assert citations[0]["cited"] is True


def test_select_falls_back_to_all_when_no_marker():
    """模型未按约定标注时，回退为全部检索资料（标记未直接引用），
    避免出现「答案明显来自知识库却没有任何来源」。"""
    chunks = [make_chunk("第一段"), make_chunk("第二段")]

    citations = select_citations(chunks, "这是一段没有编号的回答。")

    assert len(citations) == 2
    assert all(c["cited"] is False for c in citations)


def test_select_returns_empty_without_chunks():
    """没有检索资料时不应产生任何引用。"""
    assert select_citations([], "随便一段回答") == []


def test_citation_payload_has_jump_fields():
    """引用项必须带跳转所需字段（doc_id + chunk_id），否则前端卡片点不开。"""
    chunk = make_chunk("正文内容", heading="常见问题", doc_title="产品说明.md")

    citation = build_citations([chunk])[0]

    assert citation["doc_id"] == chunk.doc_id
    assert citation["chunk_id"] == chunk.chunk_id
    assert citation["chunk_index"] == chunk.chunk_index
    assert citation["heading"] == "常见问题"
    assert citation["doc_title"] == "产品说明.md"
    # 摘要不超长
    assert len(citation["snippet"]) <= 121


# =====================================================================
# 三、/chat 接口契约（citations 帧 + 落库）
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
    """测试应用：覆盖 SSE 鉴权依赖（鉴权本身由 test_auth.py 覆盖）。"""
    app = create_app()
    app.dependency_overrides[get_current_user_id_sse] = lambda: DEMO_USER_ID
    return TestClient(app)


def _install_stubs(monkeypatch, answer: str, chunks, captured: dict) -> None:
    """装配 /chat 的数据库路径桩（会话 / 检索 / 生成链全部替换）。"""
    db = AsyncMock()
    monkeypatch.setattr(chat_module, "SessionLocal", lambda: _AsyncSessionCtx(db))

    async def fake_get_or_create(dbsession, user_id, conversation_id=None):
        conv = AsyncMock()
        conv.id = uuid4()
        return conv

    async def fake_load_history(dbsession, conversation_id, max_turns, token_budget):
        return []

    async def fake_append(dbsession, conversation_id, user_content, assistant_content, citations=None):
        captured["saved_citations"] = citations

    async def fake_stream(message, conversation_id=None, history=None, meta=None,
                           memory_enabled=True, context=None):
        # 分两片返回，模拟流式；引用标注放在正文里
        half = len(answer) // 2
        for piece in (answer[:half], answer[half:]):
            yield piece

    async def fake_retrieve(query, db_session):
        from app.services.retriever import MODE_RAG, RetrievalResult

        return RetrievalResult(mode=MODE_RAG, chunks=chunks, context="[1] 资料")

    monkeypatch.setattr(chat_module.svc, "get_or_create_conversation", fake_get_or_create)
    monkeypatch.setattr(chat_module.svc, "load_history_window", fake_load_history)
    monkeypatch.setattr(chat_module.svc, "append_turn", fake_append)
    monkeypatch.setattr(chat_module, "stream_answer", fake_stream)
    monkeypatch.setattr(chat_module, "retrieve_for_chat", fake_retrieve)


def _frames(resp) -> list:
    """收集非空 SSE 行。"""
    return [line for line in resp.iter_lines() if line]


def _json_frames(frames: list) -> list:
    """把 data: {...} 行解析为 (索引, 字典)。"""
    out = []
    for i, f in enumerate(frames):
        if not f.startswith("data: "):
            continue
        try:
            obj = json.loads(f[len("data: "):])
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append((i, obj))
    return out


def test_chat_emits_citations_after_content_before_done(client, monkeypatch):
    """citations 帧必须位于最后一个正文分片之后、[DONE] 之前。"""
    captured: dict = {}
    chunks = [make_chunk("第一段资料", doc_title="A.md"), make_chunk("第二段资料", doc_title="B.md")]
    _install_stubs(monkeypatch, "结论来自第二段资料[2]。", chunks, captured)

    with client.stream("GET", "/chat", params={"message": "问题"}) as resp:
        frames = _frames(resp)

    parsed = _json_frames(frames)
    idx_last_content = max(i for i, obj in parsed if "content" in obj)
    idx_citations = next(i for i, obj in parsed if obj.get("type") == "citations")
    idx_done = frames.index("data: [DONE]")

    assert idx_last_content < idx_citations < idx_done


def test_chat_citations_only_include_used_chunk(client, monkeypatch):
    """回答只标注 [2]，则 citations 帧里只应出现第 2 段。"""
    captured: dict = {}
    chunks = [make_chunk("第一段资料", doc_title="A.md"), make_chunk("第二段资料", doc_title="B.md")]
    _install_stubs(monkeypatch, "结论来自第二段资料[2]。", chunks, captured)

    with client.stream("GET", "/chat", params={"message": "问题"}) as resp:
        frames = _frames(resp)

    citations_frame = next(
        obj for _, obj in _json_frames(frames) if obj.get("type") == "citations"
    )
    assert len(citations_frame["items"]) == 1
    item = citations_frame["items"][0]
    assert item["index"] == 2
    assert item["doc_title"] == "B.md"
    assert item["cited"] is True
    # 跳转字段齐全
    assert item["doc_id"] and item["chunk_id"]


def test_chat_persists_citations_with_message(client, monkeypatch):
    """引用关系必须随助手消息落库（messages.citations），否则历史回看不到来源。"""
    captured: dict = {}
    chunks = [make_chunk("第一段资料", doc_title="A.md")]
    _install_stubs(monkeypatch, "据资料可知[1]。", chunks, captured)

    with client.stream("GET", "/chat", params={"message": "问题"}) as resp:
        _frames(resp)

    saved = captured.get("saved_citations") or []
    assert len(saved) == 1
    assert saved[0]["index"] == 1
    assert saved[0]["doc_id"]


def test_chat_skips_citations_frame_without_hits(client, monkeypatch):
    """无检索资料（未命中/未启用）时不下发 citations 帧，避免前端渲染空区块。"""
    captured: dict = {}
    _install_stubs(monkeypatch, "通用回答，没有引用。", [], captured)

    with client.stream("GET", "/chat", params={"message": "今天天气"}) as resp:
        frames = _frames(resp)

    assert not any(obj.get("type") == "citations" for _, obj in _json_frames(frames))
    # 落库时引用为空（None），表示「本轮没有来源」
    assert captured.get("saved_citations") == []


# =====================================================================
# 四、历史回显（messages.citations → 接口响应）
# =====================================================================


def test_parse_citations_skips_invalid_items():
    """历史数据里的脏结构应被跳过而不是打挂接口。"""
    from app.api.conversations import _parse_citations

    valid = {
        "index": 1,
        "doc_id": "d1",
        "doc_title": "A.md",
        "chunk_id": "c1",
        "snippet": "片段",
    }

    parsed = _parse_citations([valid, {"index": "不是数字"}, "不是一个对象", None])

    assert len(parsed) == 1
    assert parsed[0].doc_id == "d1"


@pytest.mark.parametrize("raw", [None, {}, "字符串", 123])
def test_parse_citations_handles_non_list(raw):
    """citations 非列表（旧数据 / None）时返回空列表，不抛异常。"""
    from app.api.conversations import _parse_citations

    assert _parse_citations(raw) == []


# =====================================================================
# 五、引用跳转接口（GET /documents/{doc_id}/chunks/{chunk_id}）
# =====================================================================


class _Result:
    """模拟 SQLAlchemy Result：支持 scalar_one_or_none / all。"""

    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeDb:
    """按调用顺序返回预置结果的假会话（接口内固定执行 3 次查询）。"""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, *args, **kwargs):
        return self._results.pop(0)


class _Row:
    """模拟只选了两列的查询结果行（id / chunk_index）。"""

    def __init__(self, id, chunk_index=0, metadata_json=None, content=""):
        self.id = id
        self.chunk_index = chunk_index
        self.metadata_json = metadata_json or {}
        self.content = content


class _Doc:
    """文档桩。"""

    def __init__(self, doc_id, filename="A.md", file_type="markdown"):
        self.id = doc_id
        self.filename = filename
        self.file_type = file_type


@pytest.fixture()
def chunk_client():
    """为跳转接口准备客户端：覆盖鉴权依赖，DB 用按需注入的假会话。"""
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: DEMO_USER_ID
    return app, TestClient(app)


def _override_db(app, results):
    """把 get_db 依赖替换为返回指定假会话。"""
    db = _FakeDb(results)
    app.dependency_overrides[get_db] = lambda: db
    return db


def test_chunk_detail_returns_content_and_neighbors(chunk_client):
    """正常路径：返回完整正文、总段数与相邻片段 ID。"""
    app, client = chunk_client
    doc_id, c1, c2, c3 = uuid4(), uuid4(), uuid4(), uuid4()
    doc = _Doc(doc_id, filename="产品说明.md")
    target = _Row(c2, chunk_index=1, metadata_json={"heading": "功能特性"}, content="完整正文内容")

    _override_db(
        app,
        [
            _Result([doc]),  # 1) 文档校验
            _Result([_Row(c1, 0), _Row(c2, 1), _Row(c3, 2)]),  # 2) 全部切片顺序
            _Result([target]),  # 3) 目标切片
        ],
    )
    try:
        resp = client.get(f"/documents/{doc_id}/chunks/{c2}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "完整正文内容"
    assert body["total_chunks"] == 3
    assert body["heading"] == "功能特性"
    assert body["prev_chunk_id"] == str(c1)
    assert body["next_chunk_id"] == str(c3)
    assert body["doc_title"] == "产品说明.md"


def test_chunk_detail_rejects_invalid_uuid(chunk_client):
    """非法 ID 格式 → 404（与「不存在」同语义，不泄露 ID 规则）。"""
    _, client = chunk_client

    resp = client.get("/documents/not-a-uuid/chunks/also-bad")

    assert resp.status_code == 404


def test_chunk_detail_404_when_document_not_indexed(chunk_client):
    """未入库 / 已下线文档不可被引用查看（查询条件过滤后取不到文档）。"""
    app, client = chunk_client
    doc_id, chunk_id = uuid4(), uuid4()
    _override_db(app, [_Result([])])  # 文档查询为空
    try:
        resp = client.get(f"/documents/{doc_id}/chunks/{chunk_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 404


def test_chunk_detail_404_when_chunk_not_in_document(chunk_client):
    """切片不属于该文档（如已被重入库替换）→ 404。"""
    app, client = chunk_client
    doc_id, other, chunk_id = uuid4(), uuid4(), uuid4()
    _override_db(
        app,
        [
            _Result([_Doc(doc_id)]),
            _Result([_Row(other, 0)]),  # 该文档只有另一个切片
        ],
    )
    try:
        resp = client.get(f"/documents/{doc_id}/chunks/{chunk_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 404
