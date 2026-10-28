# 答案反馈测试（P6-B2）
# ---------------------------------------------------------------------
# 原则：与既有测试一致——服务层用真实对象（内存桩），接口层用 AsyncMock 替代数据库、
#      monkeypatch 服务函数，只验证「契约与状态机」，不依赖 PostgreSQL、不产生费用。
#
# 覆盖重点（对应《需求清单》P6 功能2「答案反馈与评估」）：
#   1. 归属与角色校验：只能评价「本人会话里的助手回答」；
#   2. 入参规范化：点赞不留归因、非法规因/超长备注被拒绝；
#   3. upsert：同一用户对同一条回答只保留一条反馈（重复提交=修改）；
#   4. 取消反馈：物理删除且幂等；
#   5. 历史回显：批量取反馈用于会话详情；
#   6. 评估统计：好评率、归因分布、最近差评的上下文配对；
#   7. 鉴权：401 / 403（管理员接口）/ 404 / 409 的映射。

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.feedback as feedback_module
from app.core import security as security_module
from app.core.errors import FeedbackNotAllowed, MessageNotFound
from app.db.models import Feedback, Message, User
from app.main import create_app
from app.services import feedback_service as svc


# ---------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------


def _make_user(role="user"):
    """构造一个内存用户对象（不落库）。"""
    user = User()
    user.id = uuid4()
    user.username = f"{role}-{uuid4().hex[:6]}"
    user.role = role
    user.is_active = True
    return user


def _make_message(role="assistant", content="这是回答", conversation_id=None, at=None):
    """构造一个内存消息对象（不落库）。"""
    msg = Message()
    msg.id = uuid4()
    msg.conversation_id = conversation_id or uuid4()
    msg.role = role
    msg.content = content
    msg.created_at = at or datetime(2026, 10, 1, tzinfo=timezone.utc)
    return msg


def _make_feedback(message_id, user_id, rating="down", reason="wrong", comment=None):
    """构造一个内存反馈对象（不落库）。"""
    fb = Feedback()
    fb.id = uuid4()
    fb.message_id = message_id
    fb.user_id = user_id
    fb.rating = rating
    fb.reason = reason
    fb.comment = comment
    fb.created_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    fb.updated_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return fb


def _result(scalar=None, all_rows=None, scalars_all=None):
    """构造一个 Result 桩，同时支持 scalar / scalar_one_or_none / all / scalars().all()。

    Args:
        scalar: 单值返回值（计数、单对象查询）。
        all_rows: `.all()` 的返回值（分组统计用的元组列表）。
        scalars_all: `.scalars().all()` 的返回值（ORM 对象列表）。
    """
    res = MagicMock()
    res.scalar.return_value = scalar
    res.scalar_one_or_none.return_value = scalar
    res.all.return_value = all_rows if all_rows is not None else []
    res.scalars.return_value.all.return_value = scalars_all if scalars_all is not None else []
    return res


def _db(*results):
    """构造按顺序返回预设 Result 的 AsyncMock 数据库。

    注意：add 是同步方法，必须显式替换为 MagicMock，
    否则 AsyncMock 会把它包成协程，既不生效又产生未等待协程告警。
    """
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


# ---------------------------------------------------------------------
# 服务层：提交反馈
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_up_ignores_reason():
    """点赞不应保留归因——点赞没有「原因」语义，留着会污染归因统计。"""
    user_id = uuid4()
    answer = _make_message()
    # ① 取消息（归属+角色校验）② 查已有反馈（无）
    db = _db(_result(scalar=answer), _result(scalar=None))

    fb = await svc.submit_feedback(
        db, user_id, answer.id, "up", reason="wrong", comment="不错"
    )

    assert fb.rating == "up"
    assert fb.reason is None
    # 点赞仍可保留文字评价
    assert fb.comment == "不错"
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_down_keeps_reason_and_comment():
    """点踩应保存归因与补充说明（离线优化的关键输入）。"""
    user_id = uuid4()
    answer = _make_message()
    db = _db(_result(scalar=answer), _result(scalar=None))

    fb = await svc.submit_feedback(
        db, user_id, answer.id, "down", reason="off_topic", comment="  答的不是我问的  "
    )

    assert fb.rating == "down"
    assert fb.reason == "off_topic"
    # 首尾空白应被清理
    assert fb.comment == "答的不是我问的"


@pytest.mark.asyncio
async def test_submit_rejects_non_assistant_message():
    """只能评价助手回答：对用户提问打分没有业务含义。"""
    user_id = uuid4()
    question = _make_message(role="user", content="我的问题")
    db = _db(_result(scalar=question))

    with pytest.raises(FeedbackNotAllowed):
        await svc.submit_feedback(db, user_id, question.id, "up")


@pytest.mark.asyncio
async def test_submit_rejects_other_users_message():
    """别人的回答不可评价（返回「找不到」，不泄露存在性）。"""
    db = _db(_result(scalar=None))

    with pytest.raises(MessageNotFound):
        await svc.submit_feedback(db, uuid4(), uuid4(), "up")


@pytest.mark.asyncio
async def test_submit_rejects_invalid_rating():
    """非法评价结论快速失败（服务层可能被后台脚本直接调用）。"""
    db = _db()
    with pytest.raises(FeedbackNotAllowed):
        await svc.submit_feedback(db, uuid4(), uuid4(), "meh")


@pytest.mark.asyncio
async def test_submit_rejects_invalid_reason():
    """归因不在白名单内应被拒绝，避免污染统计口径。"""
    user_id = uuid4()
    answer = _make_message()
    db = _db(_result(scalar=answer))

    with pytest.raises(FeedbackNotAllowed):
        await svc.submit_feedback(db, user_id, answer.id, "down", reason="whatever")


@pytest.mark.asyncio
async def test_submit_rejects_overlong_comment():
    """补充说明超长应拒绝（防止超长文本进库）。"""
    user_id = uuid4()
    answer = _make_message()
    db = _db(_result(scalar=answer))

    with pytest.raises(FeedbackNotAllowed):
        await svc.submit_feedback(db, user_id, answer.id, "down", comment="x" * 2000)


@pytest.mark.asyncio
async def test_submit_updates_existing_feedback():
    """重复提交是「修改」而不是新增（先赞后踩很常见）。"""
    user_id = uuid4()
    answer = _make_message()
    existing = _make_feedback(answer.id, user_id, rating="up", reason=None)
    # ① 取消息 ② 命中已有反馈
    db = _db(_result(scalar=answer), _result(scalar=existing))

    fb = await svc.submit_feedback(db, user_id, answer.id, "down", reason="unsupported")

    assert fb is existing
    assert fb.rating == "down"
    assert fb.reason == "unsupported"
    # 修改路径不应再 add 一条新记录
    db.add.assert_not_called()
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------
# 服务层：查询 / 取消 / 批量回显
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_feedback_returns_none_for_invalid_id():
    """ID 非法时按「未评价」处理，不让历史页面整体报错。"""
    db = _db()
    assert await svc.get_feedback(db, uuid4(), "not-a-uuid") is None


@pytest.mark.asyncio
async def test_delete_feedback_returns_false_when_absent():
    """没有反馈时取消操作幂等返回 False（接口仍 204）。"""
    db = _db(_result(scalar=None))
    assert await svc.delete_feedback(db, uuid4(), uuid4()) is False


@pytest.mark.asyncio
async def test_delete_feedback_removes_record():
    """有反馈时应物理删除并提交。"""
    user_id = uuid4()
    fb = _make_feedback(uuid4(), user_id)
    db = _db(_result(scalar=fb))

    assert await svc.delete_feedback(db, user_id, fb.message_id) is True
    db.delete.assert_awaited_once_with(fb)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_feedback_map_returns_mapping():
    """批量回显应返回 {message_id: feedback} 映射。"""
    user_id = uuid4()
    mid = uuid4()
    fb = _make_feedback(mid, user_id)
    db = _db(_result(scalars_all=[fb]))

    result = await svc.load_feedback_map(db, user_id, [mid])

    assert result == {mid: fb}


@pytest.mark.asyncio
async def test_load_feedback_map_skips_empty_ids():
    """无消息 ID 时不应查库。"""
    db = _db()
    assert await svc.load_feedback_map(db, uuid4(), []) == {}
    db.execute.assert_not_awaited()


# ---------------------------------------------------------------------
# 服务层：评估统计
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_summary_counts_and_rate():
    """统计应给出总数、赞踩计数与好评率（分母为 0 时记 0）。"""
    # ① 赞踩计数 ② 归因分布 ③ 最近差评 ④ 回答消息 ⑤ 用户提问
    db = _db(
        _result(all_rows=[("up", 3), ("down", 1)]),
        _result(all_rows=[("off_topic", 1)]),
        _result(scalars_all=[]),
    )

    summary = await svc.build_summary(db)

    assert summary["total"] == 4
    assert summary["up_count"] == 3
    assert summary["down_count"] == 1
    assert summary["up_rate"] == 0.75
    assert summary["reason_breakdown"] == {"off_topic": 1}
    assert summary["recent_down"] == []


@pytest.mark.asyncio
async def test_build_summary_empty_dataset():
    """没有任何反馈时不应除零，各项计数为 0。"""
    db = _db(_result(all_rows=[]), _result(all_rows=[]), _result(scalars_all=[]))

    summary = await svc.build_summary(db)

    assert summary["total"] == 0
    assert summary["up_rate"] == 0.0
    assert summary["reason_breakdown"] == {}


@pytest.mark.asyncio
async def test_attach_context_pairs_question_with_answer():
    """差评明细应配上「该回答之前最近的一条用户提问」。"""
    conv_id = uuid4()
    t0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    q1 = _make_message(role="user", content="第一个问题", conversation_id=conv_id, at=t0)
    answer = _make_message(
        content="第一个回答", conversation_id=conv_id, at=t0 + timedelta(seconds=5)
    )
    q2 = _make_message(
        role="user", content="第二个问题", conversation_id=conv_id, at=t0 + timedelta(seconds=30)
    )
    fb = _make_feedback(answer.id, uuid4(), rating="down", reason="wrong", comment="答错了")

    # ① 最近差评 ② 回答消息 ③ 该会话的用户提问
    db = _db(
        _result(all_rows=[("down", 1)]),
        _result(all_rows=[]),
        _result(scalars_all=[fb]),
        _result(scalars_all=[answer]),
        _result(scalars_all=[q1, q2]),
    )

    summary = await svc.build_summary(db)

    assert len(summary["recent_down"]) == 1
    item = summary["recent_down"][0]
    # 必须配到「回答之前」的那条提问，而不是会话里最新的那条
    assert item["question"] == "第一个问题"
    assert item["answer"] == "第一个回答"
    assert item["comment"] == "答错了"


def test_pick_question_prefers_latest_before_answer():
    """_pick_question 的时间配对规则（纯函数，单独锁死）。"""
    conv_id = uuid4()
    t0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    before = _make_message(role="user", content="旧问题", conversation_id=conv_id, at=t0)
    answer = _make_message(conversation_id=conv_id, at=t0 + timedelta(seconds=10))
    after = _make_message(
        role="user", content="新问题", conversation_id=conv_id, at=t0 + timedelta(seconds=20)
    )

    assert svc._pick_question([before, after], answer) == "旧问题"


def test_pick_question_falls_back_when_no_time_info():
    """时间戳缺失时退化为最后一条用户消息（宁可配错也不要空白）。"""
    q1 = _make_message(role="user", content="问题一")
    q2 = _make_message(role="user", content="问题二")
    answer = _make_message()
    # 清空时间，模拟历史脏数据
    q1.created_at = None
    q2.created_at = None
    answer.created_at = None

    assert svc._pick_question([q1, q2], answer) == "问题二"


# ---------------------------------------------------------------------
# 接口层：鉴权与契约
# ---------------------------------------------------------------------


def test_feedback_endpoints_require_auth():
    """未携带令牌访问反馈接口应 401。"""
    client = TestClient(create_app())
    mid = uuid4()
    assert client.post(f"/messages/{mid}/feedback", json={"rating": "up"}).status_code == 401
    assert client.get(f"/messages/{mid}/feedback").status_code == 401
    assert client.delete(f"/messages/{mid}/feedback").status_code == 401
    assert client.get("/admin/feedback/summary").status_code == 401


@pytest.fixture()
def user_client():
    """覆盖鉴权与数据库依赖，返回（TestClient, 当前用户, db 占位）。"""
    app = create_app()
    user = _make_user("user")
    db = AsyncMock()
    app.dependency_overrides[feedback_module.get_current_user_id] = lambda: user.id
    app.dependency_overrides[feedback_module.get_db] = lambda: db
    return TestClient(app), user, db


def test_submit_feedback_returns_payload(user_client, monkeypatch):
    """提交成功应返回规范化的反馈结构。"""
    client, user, _ = user_client
    mid = uuid4()
    fb = _make_feedback(mid, user.id, rating="down", reason="off_topic", comment="跑题了")

    async def fake_submit(**kwargs):
        return fb

    monkeypatch.setattr(feedback_module.svc, "submit_feedback", fake_submit)

    resp = client.post(
        f"/messages/{mid}/feedback",
        json={"rating": "down", "reason": "off_topic", "comment": "跑题了"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["message_id"] == str(mid)
    assert body["rating"] == "down"
    assert body["reason"] == "off_topic"


def test_submit_feedback_invalid_rating_returns_422(user_client):
    """rating 只允许 up / down（Pydantic 层拦截，不进服务层）。"""
    client, _, _ = user_client
    resp = client.post(f"/messages/{uuid4()}/feedback", json={"rating": "meh"})
    assert resp.status_code == 422


def test_submit_feedback_not_found_maps_to_404(user_client, monkeypatch):
    """消息不属于当前用户 → 404（不区分「不存在」与「不是你的」）。"""
    client, _, _ = user_client

    async def fake_submit(**kwargs):
        raise MessageNotFound("x")

    monkeypatch.setattr(feedback_module.svc, "submit_feedback", fake_submit)

    resp = client.post(f"/messages/{uuid4()}/feedback", json={"rating": "up"})
    assert resp.status_code == 404


def test_submit_feedback_not_allowed_maps_to_409(user_client, monkeypatch):
    """对非助手消息评价 → 409（状态不允许）。"""
    client, _, _ = user_client

    async def fake_submit(**kwargs):
        raise FeedbackNotAllowed("只能对助手回答进行评价")

    monkeypatch.setattr(feedback_module.svc, "submit_feedback", fake_submit)

    resp = client.post(f"/messages/{uuid4()}/feedback", json={"rating": "up"})
    assert resp.status_code == 409


def test_get_feedback_returns_null_when_absent(user_client, monkeypatch):
    """未评价时返回 feedback=null，前端据此把按钮置为未选中。"""
    client, _, _ = user_client
    mid = uuid4()

    async def fake_get(db, uid, message_id):
        return None

    monkeypatch.setattr(feedback_module.svc, "get_feedback", fake_get)

    resp = client.get(f"/messages/{mid}/feedback")
    assert resp.status_code == 200
    assert resp.json() == {"message_id": str(mid), "feedback": None}


def test_delete_feedback_returns_204(user_client, monkeypatch):
    """取消反馈是幂等的 204（没有记录也成功）。"""
    client, _, _ = user_client

    async def fake_delete(db, uid, message_id):
        return False

    monkeypatch.setattr(feedback_module.svc, "delete_feedback", fake_delete)

    resp = client.delete(f"/messages/{uuid4()}/feedback")
    assert resp.status_code == 204


def test_admin_summary_forbidden_for_normal_user(user_client, monkeypatch):
    """普通用户访问评估概览应 403（RBAC 由后端裁决）。"""
    client, _, _ = user_client
    app = client.app
    # require_admin 内部依赖 core.security.get_current_user，
    # 这里只覆盖「当前用户」为普通角色，让 require_admin 走真实的角色校验分支
    app.dependency_overrides[security_module.get_current_user] = lambda: _make_user("user")

    resp = client.get("/admin/feedback/summary")
    assert resp.status_code == 403


def test_admin_summary_returns_stats(user_client, monkeypatch):
    """管理员可获取评估概览（计数 + 归因 + 最近差评）。"""
    client, _, _ = user_client
    admin = _make_user("admin")
    client.app.dependency_overrides[feedback_module.require_admin] = lambda: admin

    async def fake_summary(db, recent_limit=10):
        return {
            "total": 2,
            "up_count": 1,
            "down_count": 1,
            "up_rate": 0.5,
            "reason_breakdown": {"wrong": 1},
            "recent_down": [
                {
                    "feedback_id": str(uuid4()),
                    "message_id": str(uuid4()),
                    "conversation_id": str(uuid4()),
                    "question": "问题",
                    "answer": "回答",
                    "reason": "wrong",
                    "comment": None,
                    "created_at": "2026-10-01T00:00:00+00:00",
                }
            ],
        }

    monkeypatch.setattr(feedback_module.svc, "build_summary", fake_summary)

    resp = client.get("/admin/feedback/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["up_rate"] == 0.5
    assert body["recent_down"][0]["reason"] == "wrong"


# ---------------------------------------------------------------------
# 接口层：/chat 的 message 帧（前端反馈按钮的前置条件）
# ---------------------------------------------------------------------


class _AsyncSessionCtx:
    """让 monkeypatch 后的 SessionLocal() 支持 `async with` 并返回假会话。"""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _parse_frames(frames):
    """把 SSE 帧文本解析为「类型 → 载荷」列表（非 JSON 帧跳过）。"""
    import json

    parsed = []
    for f in frames:
        if not f.startswith("data: "):
            continue
        raw = f[len("data: "):]
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            parsed.append(obj)
    return parsed


def test_chat_emits_message_frame_after_persist(monkeypatch):
    """落库后必须下发 message 帧，否则前端拿不到后端消息 ID，反馈按钮永远不出现。"""
    import app.api.chat as chat_module
    from app.core.security import DEMO_USER_ID, get_current_user_id_sse
    from app.services.retriever import MODE_DISABLED, RetrievalResult

    app = create_app()
    # /chat 的鉴权走 SSE 专用依赖（兼容 query 令牌），这里直接覆盖成固定用户
    app.dependency_overrides[get_current_user_id_sse] = lambda: DEMO_USER_ID
    client = TestClient(app)

    db = AsyncMock()
    db.add = MagicMock()
    monkeypatch.setattr(chat_module, "SessionLocal", lambda: _AsyncSessionCtx(db))

    async def fake_retrieval(query, session):
        return RetrievalResult(mode=MODE_DISABLED, reason="测试环境未启用检索")

    async def fake_get_or_create(session, user_id, conversation_id=None):
        conv = MagicMock()
        conv.id = uuid4()
        return conv

    async def fake_load_history(session, conversation_id, max_turns, token_budget):
        return []

    user_msg_id, answer_msg_id = uuid4(), uuid4()

    async def fake_append(session, conversation_id, user_content, assistant_content, citations=None):
        # 模拟 conversation_service.append_turn：回传两条消息主键
        return user_msg_id, answer_msg_id

    async def fake_stream(message, conversation_id=None, history=None, meta=None,
                          memory_enabled=True, context=None):
        if isinstance(meta, dict):
            meta["finish_reason"] = "stop"
        yield "回答内容"

    monkeypatch.setattr(chat_module, "retrieve_for_chat", fake_retrieval)
    monkeypatch.setattr(chat_module.svc, "get_or_create_conversation", fake_get_or_create)
    monkeypatch.setattr(chat_module.svc, "load_history_window", fake_load_history)
    monkeypatch.setattr(chat_module.svc, "append_turn", fake_append)
    monkeypatch.setattr(chat_module, "stream_answer", fake_stream)

    with client.stream("GET", "/chat", params={"message": "你好"}) as resp:
        frames = [line for line in resp.iter_lines() if line]

    payloads = _parse_frames(frames)
    message_frames = [p for p in payloads if p.get("type") == "message"]
    assert len(message_frames) == 1, payloads
    assert message_frames[0]["id"] == str(answer_msg_id)
    assert message_frames[0]["user_message_id"] == str(user_msg_id)
    # 必须在结束哨兵之前下发（否则前端已关闭连接，收不到该帧）
    assert frames[-1].strip() == "data: [DONE]"


def test_conversation_history_includes_feedback(monkeypatch):
    """会话详情应随历史回传当前用户的反馈（刷新后按钮状态才不会「失忆」）。"""
    import app.api.conversations as conv_module
    from app.core.security import DEMO_USER_ID

    app = create_app()
    app.dependency_overrides[conv_module.get_current_user_id] = lambda: DEMO_USER_ID
    app.dependency_overrides[conv_module.get_db] = lambda: AsyncMock()

    client = TestClient(app)
    cid = uuid4()
    answer = _make_message(content="回答正文")
    question = _make_message(role="user", content="提问")
    fb = _make_feedback(answer.id, DEMO_USER_ID, rating="down", reason="wrong")

    # 会话与消息用轻量替身（接口只读取这几个字段）
    conv = type(
        "Conv",
        (),
        {
            "id": cid,
            "title": "会话",
            "model": "deepseek-flash",
            "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        },
    )()

    async def fake_detail(db, user_id, conversation_id):
        return conv, [question, answer]

    async def fake_load_map(db, user_id, message_ids):
        return {answer.id: fb}

    monkeypatch.setattr(conv_module.svc, "get_conversation_detail", fake_detail)
    monkeypatch.setattr(conv_module.feedback_service, "load_feedback_map", fake_load_map)

    resp = client.get(f"/conversations/{cid}")
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    # 用户提问没有反馈；助手回答带着点踩记录
    assert messages[0]["feedback"] is None
    assert messages[1]["feedback"]["rating"] == "down"
    assert messages[1]["feedback"]["reason"] == "wrong"
