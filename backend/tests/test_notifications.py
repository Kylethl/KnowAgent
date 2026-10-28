# 审核通知测试（P6-B1）
# ---------------------------------------------------------------------
# 原则：与既有测试一致——服务层用真实对象（内存桩），接口层用 AsyncMock 替代数据库、
#      monkeypatch 服务函数，只验证「契约与行为」，不依赖 PostgreSQL、不产生费用。
#
# 覆盖重点（对应《需求清单》P6 功能1「审核通知」）：
#   1. 通知文案：通过/驳回两种结论的标题与正文（含驳回理由截断）；
#   2. 触发时机：审核动作落定时同事务写入通知（不额外 commit）；
#   3. 无收件人（匿名提交）不发通知；
#   4. 列表/未读数/已读：分页规范化、未读统计、幂等已读、一键已读；
#   5. 鉴权：未登录 401，且通知只能查自己的（user_id 来自令牌）。

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.notifications as notifications_module
from app.core.errors import NotificationNotFound
from app.db.models import Document, Notification, User
from app.main import create_app
from app.services import review_service
from app.services import notification_service as svc


# ---------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------


def _make_document(user_id=None, filename="季度报告.md", review_note=None):
    """构造一个内存文档对象（不落库）。"""
    doc = Document()
    doc.id = uuid4()
    # user_id 为空表示 P1 时代的匿名提交（无收件人）
    doc.user_id = user_id
    doc.filename = filename
    doc.file_type = "markdown"
    doc.status = "pending"
    doc.review_status = "pending"
    doc.review_note = review_note
    doc.original_path = "scope/2026/10/abc.md"
    doc.size_bytes = 100
    doc.created_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return doc


def _make_notification(user_id, type_="document_approved", read=False):
    """构造一个内存通知对象（不落库）。"""
    n = Notification()
    n.id = uuid4()
    n.user_id = user_id
    n.type = type_
    n.title = "文档《a.md》已通过审核"
    n.content = "正文"
    n.doc_id = uuid4()
    n.read_at = datetime(2026, 10, 1, tzinfo=timezone.utc) if read else None
    n.created_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return n


def _make_admin():
    """构造一个内存管理员对象。"""
    user = User()
    user.id = uuid4()
    user.username = "admin"
    user.role = "admin"
    user.is_active = True
    return user


def _db_with_results(*pairs):
    """构造 AsyncMock 数据库，按调用顺序返回预设的 Result 桩（同既有测试写法）。"""
    db = AsyncMock()
    # add 是**同步**方法：若不显式替换，AsyncMock 会把它包成协程，
    # 调用后既不生效又会产生「coroutine was never awaited」告警。
    db.add = MagicMock()
    results = []
    for scalar_value, all_value in pairs:
        res = MagicMock()
        res.scalar.return_value = scalar_value
        res.scalar_one_or_none.return_value = scalar_value
        res.scalars.return_value.all.return_value = all_value
        results.append(res)
    db.execute = AsyncMock(side_effect=results)
    return db


# ---------------------------------------------------------------------
# 服务层：通知文案
# ---------------------------------------------------------------------


def test_build_message_for_approved():
    """通过通知应说明「已通过 + 正在入库」，并附上可选备注。"""
    doc = _make_document(review_note="内容完整")
    title, content = svc.build_review_message(doc, "approved")

    assert "季度报告.md" in title and "通过" in title
    # 用户最关心「能不能用上了」，因此正文必须交代入库这一步
    assert "入库" in content
    assert "内容完整" in content


def test_build_message_for_rejected_includes_reason():
    """驳回通知必须带驳回理由（否则用户不知道该怎么改）。"""
    doc = _make_document(review_note="缺少必要章节")
    title, content = svc.build_review_message(doc, "rejected")

    assert "驳回" in title
    assert "缺少必要章节" in content


def test_build_message_rejected_without_note_falls_back():
    """历史数据可能没写理由：正文给出兜底文案而不是空白。"""
    doc = _make_document(review_note=None)
    _, content = svc.build_review_message(doc, "rejected")
    assert "未填写具体理由" in content


def test_build_message_truncates_long_note():
    """超长理由应被截断，避免一条通知把前端面板撑爆。"""
    doc = _make_document(review_note="很长的理由" * 200)
    _, content = svc.build_review_message(doc, "rejected")
    assert len(content) < 400


def test_build_message_rejects_unknown_action():
    """非法动作应快速失败，而不是发一条语义不明的通知。"""
    with pytest.raises(ValueError):
        svc.build_review_message(_make_document(), "deleted")


# ---------------------------------------------------------------------
# 服务层：通知生成
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_notification_skips_anonymous_document():
    """无提交人（P1 匿名提交）时不应写入通知——没人可发。"""
    db = _db_with_results()
    result = await svc.create_review_notification(db, _make_document(user_id=None), "approved")

    assert result is None
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_notification_adds_without_commit():
    """通知只 add 不 commit：与审核结果同事务，避免「审核成功但通知丢失」。"""
    db = _db_with_results()
    user_id = uuid4()
    notification = await svc.create_review_notification(
        db, _make_document(user_id=user_id), "rejected"
    )

    assert isinstance(notification, Notification)
    assert notification.user_id == user_id
    assert notification.type == "document_rejected"
    db.add.assert_called_once_with(notification)
    # 关键：服务层不得自行提交，事务边界归调用方
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_review_document_triggers_notification():
    """审核动作落定时应把通知加入同一事务（P6-B1 的触发点）。"""
    user_id = uuid4()
    doc = _make_document(user_id=user_id)
    db = _db_with_results()

    await review_service.review_document(db, doc, _make_admin(), "approved", note="ok")

    # 审核字段与通知在同一次 add 序列中，且只提交一次
    added = [call.args[0] for call in db.add.call_args_list]
    assert any(isinstance(obj, Notification) for obj in added)
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------
# 服务层：查询与已读
# ---------------------------------------------------------------------


def test_normalize_pagination_clamps_values():
    """分页参数应被收敛到安全区间（上限 50、下限 1、负偏移归零）。"""
    assert svc.normalize_pagination(None, None) == (20, 0)
    assert svc.normalize_pagination(0, -3) == (1, 0)
    assert svc.normalize_pagination(999, 5) == (50, 5)
    assert svc.normalize_pagination("x", "y") == (20, 0)


@pytest.mark.asyncio
async def test_list_notifications_returns_items_total_unread():
    """列表应返回（当前页, 总数, 未读数），三者口径各自独立。"""
    user_id = uuid4()
    n1 = _make_notification(user_id)
    # 三次 execute：① count ② 列表 ③ 未读数统计
    db = _db_with_results((5, []), (None, [n1]), (2, []))

    items, total, unread = await svc.list_notifications(db, user_id=user_id, limit=10)

    assert total == 5
    assert unread == 2
    assert [n.id for n in items] == [n1.id]
    assert db.execute.await_count == 3


@pytest.mark.asyncio
async def test_mark_read_is_idempotent():
    """已读通知再次标记不应改写时间（幂等）。"""
    user_id = uuid4()
    already = _make_notification(user_id, read=True)
    original = already.read_at
    db = _db_with_results((already, []))

    result = await svc.mark_read(db, user_id, already.id)

    assert result.read_at == original
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_mark_read_sets_timestamp():
    """未读通知标记后应写入已读时间并提交。"""
    user_id = uuid4()
    unread = _make_notification(user_id, read=False)
    db = _db_with_results((unread, []))

    result = await svc.mark_read(db, user_id, str(unread.id))

    assert result.read_at is not None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_read_raises_when_not_found():
    """通知不存在 / 不属于当前用户 → NotificationNotFound（接口层 404）。"""
    db = _db_with_results((None, []))
    with pytest.raises(NotificationNotFound):
        await svc.mark_read(db, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_mark_read_raises_on_invalid_id():
    """ID 格式非法同样按「找不到」处理，不暴露库内细节。"""
    db = _db_with_results()
    with pytest.raises(NotificationNotFound):
        await svc.mark_read(db, uuid4(), "not-a-uuid")


@pytest.mark.asyncio
async def test_mark_all_read_returns_rowcount():
    """一键已读应返回实际更新条数。"""
    db = _db_with_results()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=3))

    updated = await svc.mark_all_read(db, uuid4())

    assert updated == 3
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------
# 接口层：鉴权与契约
# ---------------------------------------------------------------------


def test_notification_endpoints_require_auth():
    """未携带令牌访问通知接口应 401（通知是私信，不允许匿名读）。"""
    client = TestClient(create_app())
    assert client.get("/notifications").status_code == 401
    assert client.get("/notifications/unread-count").status_code == 401
    assert client.post(f"/notifications/{uuid4()}/read").status_code == 401
    assert client.post("/notifications/read-all").status_code == 401


@pytest.fixture()
def notif_client():
    """覆盖鉴权与数据库依赖，返回（TestClient, user_id, db 占位）。"""
    app = create_app()
    user_id = uuid4()
    db = AsyncMock()
    app.dependency_overrides[notifications_module.get_current_user_id] = lambda: user_id
    app.dependency_overrides[notifications_module.get_db] = lambda: db
    return TestClient(app), user_id, db


def test_list_notifications_returns_payload(notif_client, monkeypatch):
    """列表接口应返回 items/total/unread_count/limit/offset 结构。"""
    client, user_id, _ = notif_client
    n1 = _make_notification(user_id, type_="document_rejected")

    async def fake_list(db, user_id, unread_only=False, limit=None, offset=None):
        return [n1], 1, 1

    monkeypatch.setattr(notifications_module.svc, "list_notifications", fake_list)

    resp = client.get("/notifications?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["unread_count"] == 1
    assert body["items"][0]["type"] == "document_rejected"
    # 已读为空 → 前端渲染未读圆点
    assert body["items"][0]["read_at"] is None


def test_mark_read_returns_unread_count(notif_client, monkeypatch):
    """标记已读应同时回传最新未读数（前端不必再请求一次）。"""
    client, user_id, _ = notif_client
    n1 = _make_notification(user_id, read=True)

    async def fake_mark(db, uid, nid):
        return n1

    async def fake_count(db, uid):
        return 0

    monkeypatch.setattr(notifications_module.svc, "mark_read", fake_mark)
    monkeypatch.setattr(notifications_module.svc, "count_unread", fake_count)

    resp = client.post(f"/notifications/{n1.id}/read")
    assert resp.status_code == 200
    assert resp.json()["unread_count"] == 0


def test_mark_read_not_found_maps_to_404(notif_client, monkeypatch):
    """通知不存在 → 404（不区分「不存在」与「不是你的」）。"""
    client, _, _ = notif_client

    async def fake_mark(db, uid, nid):
        raise NotificationNotFound(nid)

    monkeypatch.setattr(notifications_module.svc, "mark_read", fake_mark)

    resp = client.post(f"/notifications/{uuid4()}/read")
    assert resp.status_code == 404


def test_mark_all_read_returns_updated(notif_client, monkeypatch):
    """一键已读应返回更新条数与 0 未读。"""
    client, _, _ = notif_client

    async def fake_all(db, uid):
        return 4

    monkeypatch.setattr(notifications_module.svc, "mark_all_read", fake_all)

    resp = client.post("/notifications/read-all")
    assert resp.status_code == 200
    assert resp.json() == {"updated": 4, "unread_count": 0}
