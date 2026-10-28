# 管理员审核测试（P3-B1）
# ---------------------------------------------------------------------
# 原则：与既有测试一致——服务层用真实对象（内存桩），接口层用 AsyncMock 替代数据库、
#      monkeypatch 服务函数，只验证「契约与状态机」，不依赖 PostgreSQL、不产生费用。
#
# 覆盖重点（对应《需求清单》P3 功能1）：
#   1. 审核队列：筛选条件透传、分页参数规范化；
#   2. 审核状态机：通过 / 驳回 / 驳回必填理由 / 已通过为终态；
#   3. 预览：markdown 直接解、word 提取文本、超长截断；
#   4. RBAC：无令牌 401、非管理员 403、管理员 200。

from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.admin as admin_module
from app.core.errors import DocumentNotFound, IndexingInProgress, ReviewNotAllowed
from app.db.models import Document, User
from app.main import create_app
from app.services import review_service


# ---------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------


def _make_user(username="admin", role="admin"):
    """构造一个内存用户对象（不落库，仅用于测试）。"""
    user = User()
    user.id = uuid4()
    user.username = username
    user.role = role
    user.is_active = True
    return user


def _make_document(review_status="pending", status="pending", filename="a.md", file_type="markdown"):
    """构造一个内存文档对象（不落库）。"""
    doc = Document()
    doc.id = uuid4()
    doc.filename = filename
    doc.file_type = file_type
    doc.status = status
    doc.review_status = review_status
    doc.original_path = "scope/2026/01/abc.md"
    doc.size_bytes = 100
    doc.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return doc


class FakeStorage:
    """内存存储桩：实现 BaseStorage 的三个方法，供预览测试注入。"""

    def __init__(self, data: bytes):
        self._data = data

    async def put_object(self, key, data, content_type=None):  # 接口要求，测试中不调用
        self._data = data

    async def get_object(self, key: str) -> bytes:
        return self._data

    async def delete_object(self, key: str) -> None:
        self._data = b""


def _db_with_results(*pairs):
    """构造 AsyncMock 数据库，按调用顺序返回预设的 Result 桩。

    与 test_conversations.py 保持同一写法：Result 必须是 MagicMock
    （SQLAlchemy 2.x 中 execute() 是协程，而 Result 的方法是同步的）。
    每个 pair 对应一次 db.execute 调用：(scalar 返回值, scalars().all() 返回值)。
    """
    db = AsyncMock()
    results = []
    for scalar_value, all_value in pairs:
        res = MagicMock()
        # 计数查询用 scalar()；单对象查询用 scalar_one_or_none()
        res.scalar.return_value = scalar_value
        res.scalar_one_or_none.return_value = scalar_value
        res.scalars.return_value.all.return_value = all_value
        results.append(res)
    db.execute = AsyncMock(side_effect=results)
    return db


# ---------------------------------------------------------------------
# 服务层：分页与队列
# ---------------------------------------------------------------------


def test_normalize_pagination_clamps_values():
    """分页参数应被收敛到安全区间（上限 100、下限 1，负偏移归零）。"""
    assert review_service.normalize_pagination(None, None) == (20, 0)
    assert review_service.normalize_pagination(0, -5) == (1, 0)
    assert review_service.normalize_pagination(9999, 10) == (100, 10)
    # 非法类型（字符串）应回退默认值，不让接口 500
    assert review_service.normalize_pagination("abc", "xyz") == (20, 0)


@pytest.mark.asyncio
async def test_list_review_queue_returns_items_and_total():
    """队列查询应返回（当前页列表, 总数），并透传筛选条件。"""
    doc = _make_document()
    # 两次 execute：① count 查询（scalar=3）② 列表查询（scalars().all()=[doc]）
    db = _db_with_results((3, []), (None, [doc]))

    items, total = await review_service.list_review_queue(
        db, review_status="pending", keyword="报告", limit=10, offset=0
    )
    assert total == 3
    assert [d.filename for d in items] == ["a.md"]
    # 应执行两次查询：先 count 再列表
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_list_review_queue_rejects_bad_status():
    """非法审核状态筛选值应快速失败（ReviewNotAllowed），不拼出无意义 SQL。"""
    db = _db_with_results()
    with pytest.raises(ReviewNotAllowed):
        await review_service.list_review_queue(db, review_status="unknown")


# ---------------------------------------------------------------------
# 服务层：审核状态机
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_approve_sets_both_statuses():
    """通过时 review_status 与 status 同步为 approved，并记录审核人与时间。"""
    doc = _make_document()
    admin = _make_user()
    db = AsyncMock()

    updated = await review_service.review_document(db, doc, admin, "approved")
    assert updated.review_status == "approved"
    assert updated.status == "approved"
    assert updated.reviewer_id == admin.id
    assert updated.reviewed_at is not None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_reject_requires_note():
    """驳回未填理由应被拒绝（需求明确要求「驳回必填理由」）。"""
    doc = _make_document()
    db = AsyncMock()
    with pytest.raises(ReviewNotAllowed) as exc:
        await review_service.review_document(db, doc, _make_user(), "rejected", note="   ")
    assert "理由" in str(exc.value)


@pytest.mark.asyncio
async def test_review_reject_with_note_succeeds():
    """驳回填写理由后应写入备注与审核结论。"""
    doc = _make_document()
    db = AsyncMock()
    updated = await review_service.review_document(
        db, doc, _make_user(), "rejected", note="内容与主题无关"
    )
    assert updated.review_status == "rejected"
    assert updated.review_note == "内容与主题无关"


@pytest.mark.asyncio
async def test_review_approved_is_terminal():
    """已通过的文档是终态：再次审核或驳回都应被拒绝。"""
    doc = _make_document(review_status="approved", status="approved")
    db = AsyncMock()
    with pytest.raises(ReviewNotAllowed):
        await review_service.review_document(db, doc, _make_user(), "rejected", note="反悔")
    with pytest.raises(ReviewNotAllowed):
        await review_service.review_document(db, doc, _make_user(), "approved")


@pytest.mark.asyncio
async def test_review_rejects_unknown_action():
    """非 approved/rejected 的动作应被拒绝。"""
    doc = _make_document()
    db = AsyncMock()
    with pytest.raises(ReviewNotAllowed):
        await review_service.review_document(db, doc, _make_user(), "deleted")


@pytest.mark.asyncio
async def test_get_document_not_found_raises():
    """文档不存在时应抛 DocumentNotFound（接口层转 404）。"""
    db = _db_with_results((None, []))
    with pytest.raises(DocumentNotFound):
        await review_service.get_document_for_review(db, uuid4())


def test_get_document_invalid_id_raises_not_found():
    """ID 格式非法同样按「找不到」处理，避免暴露库内细节。"""
    db = _db_with_results()
    with pytest.raises(DocumentNotFound):
        import asyncio

        asyncio.run(review_service.get_document_for_review(db, "not-a-uuid"))


# ---------------------------------------------------------------------
# 服务层：预览
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_markdown_decodes_text():
    """Markdown 预览应直接解码出原文。"""
    doc = _make_document(file_type="markdown")
    storage = FakeStorage("# 标题\n正文内容".encode("utf-8"))
    text, truncated = await review_service.preview_document(storage, doc)
    assert "正文内容" in text
    assert truncated is False


@pytest.mark.asyncio
async def test_preview_word_extracts_paragraphs():
    """Word(.docx) 预览应用 python-docx 提取段落文本。"""
    from docx import Document as DocxDocument

    # 用 python-docx 现场造一个最小 docx，避免依赖外部样例文件
    d = DocxDocument()
    d.add_paragraph("第一段内容")
    d.add_paragraph("第二段内容")
    buf = BytesIO()
    d.save(buf)

    doc = _make_document(file_type="word", filename="a.docx")
    storage = FakeStorage(buf.getvalue())
    text, _ = await review_service.preview_document(storage, doc)
    assert "第一段内容" in text and "第二段内容" in text


@pytest.mark.asyncio
async def test_preview_truncates_long_text():
    """超长文本应被截断并标记 truncated=True。"""
    doc = _make_document()
    storage = FakeStorage(("很长" * 10000).encode("utf-8"))
    text, truncated = await review_service.preview_document(storage, doc, max_chars=100)
    assert truncated is True
    assert len(text) == 100


@pytest.mark.asyncio
async def test_preview_media_before_extraction_returns_hint():
    """媒体未解析时应返回可读提示，而不是抛错中断审核（P5 语义变更）。

    P5 起图片/音视频不再显示「暂不支持」：它们会在入库阶段经 OCR/ASR 产出文本，
    解析前预览给出「尚未解析」的说明，管理员仍可通过播放/查看原文件判断。
    """
    doc = _make_document(file_type="video")
    text, _ = await review_service.preview_document(FakeStorage(b"x"), doc)
    assert "OCR/ASR" in text


@pytest.mark.asyncio
async def test_preview_media_returns_extracted_text():
    """已解析的媒体：预览直接返回 extracted_text（不再回读原始文件）。"""
    doc = _make_document(file_type="audio")
    doc.extracted_text = "会议纪要：本次讨论了三季度目标。"
    # FakeStorage 传入的字节与结果无关：媒体预览不应读原文件
    text, truncated = await review_service.preview_document(FakeStorage(b""), doc)
    assert text.startswith("会议纪要")
    assert truncated is False


# ---------------------------------------------------------------------
# 接口层：RBAC 与契约
# ---------------------------------------------------------------------


@pytest.fixture()
def admin_client():
    """覆盖 require_admin（伪造已通过校验的管理员）与数据库依赖。"""
    app = create_app()
    admin = _make_user("admin", "admin")
    app.dependency_overrides[admin_module.require_admin] = lambda: admin
    app.dependency_overrides[admin_module.get_db] = lambda: _db_with_results()
    return TestClient(app), admin


def test_admin_endpoints_require_auth():
    """未携带令牌访问 /admin/documents 应 401（鉴权前置）。"""
    app = create_app()
    client = TestClient(app)
    resp = client.get("/admin/documents")
    assert resp.status_code == 401


def test_admin_list_returns_payload(admin_client, monkeypatch):
    """管理员查询队列应返回 items/total/limit/offset 结构。"""
    client, _ = admin_client
    doc = _make_document(filename="报告.md")

    async def fake_list(db, **kwargs):
        return [doc], 1

    monkeypatch.setattr(admin_module.review_service, "list_review_queue", fake_list)
    resp = client.get("/admin/documents", params={"review_status": "pending"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["filename"] == "报告.md"
    assert body["items"][0]["review_status"] == "pending"


def test_admin_review_returns_updated_document(admin_client, monkeypatch):
    """审核接口应回传更新后的文档条目（前端据此就地更新行）。"""
    client, admin = admin_client
    doc = _make_document()

    async def fake_get(db, document_id):
        return doc

    async def fake_review(db, doc, reviewer, action, note=None):
        doc.review_status = action
        doc.status = action
        doc.reviewer_id = reviewer.id
        return doc

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    monkeypatch.setattr(admin_module.review_service, "review_document", fake_review)

    resp = client.post(f"/admin/documents/{doc.id}/review", json={"action": "approved"})
    assert resp.status_code == 200
    assert resp.json()["document"]["review_status"] == "approved"
    # 审核人用户名应回填为当前管理员
    assert resp.json()["document"]["reviewer"] == "admin"


def test_admin_review_conflict_returns_409(admin_client, monkeypatch):
    """状态冲突（如驳回未填理由）应返回 409，并透传后端 detail。"""
    client, _ = admin_client

    async def fake_get(db, document_id):
        return _make_document()

    async def fake_review(db, doc, reviewer, action, note=None):
        raise ReviewNotAllowed("驳回时必须填写驳回理由")

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    monkeypatch.setattr(admin_module.review_service, "review_document", fake_review)

    resp = client.post(f"/admin/documents/{uuid4()}/review", json={"action": "rejected"})
    assert resp.status_code == 409
    assert "理由" in resp.json()["detail"]


def test_admin_preview_returns_text(admin_client, monkeypatch):
    """预览接口应返回 text / truncated / previewable 三要素。"""
    client, _ = admin_client
    doc = _make_document()

    async def fake_get(db, document_id):
        return doc

    async def fake_preview(storage, doc, max_chars=5000):
        return "预览正文", False

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    monkeypatch.setattr(admin_module.review_service, "preview_document", fake_preview)
    # 存储依赖注入内存桩，避免真实读写磁盘
    app = client.app
    app.dependency_overrides[admin_module.get_storage_dep] = lambda: FakeStorage(b"x")

    resp = client.get(f"/admin/documents/{doc.id}/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "预览正文"
    assert body["previewable"] is True
    assert body["truncated"] is False


def test_admin_preview_not_found_returns_404(admin_client, monkeypatch):
    """文档不存在时预览接口应返回 404。"""
    client, _ = admin_client

    async def fake_get(db, document_id):
        raise DocumentNotFound(document_id)

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    resp = client.get(f"/admin/documents/{uuid4()}/preview")
    assert resp.status_code == 404


# ---------------------------------------------------------------------
# 入库触发：POST /admin/documents/{id}/index（P5 修复补充）
# ---------------------------------------------------------------------
# 为什么补这一组：
#   此前 21 个管理员用例覆盖了 review / preview，却**没有一条**打到「重新入库」端点。
#   线上因此出了事故：文档卡在 indexing 时按钮被禁用，而这条链路上
#   「僵死放行」的分支既没被前端用到、也没被接口层测过，两侧同时失守。
# 覆盖重点：200（投递成功）/ 409（未审核、真在跑）/ 200（僵死放行，核心回归）/ 404。


def _stale_indexing_doc(minutes: float = 950):
    """构造「停留在入库中且已超时」的文档。

    对应线上现场：u_4ff45c 的《部署手册.md》status='indexing' 已持续约 16 小时。
    """
    doc = _make_document(review_status="approved", status="indexing")
    doc.updated_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return doc


def test_admin_index_dispatches_task(admin_client, monkeypatch):
    """已入库文档重新入库：应投递任务并返回 queued=True。"""
    client, _ = admin_client
    doc = _make_document(review_status="approved", status="indexed")
    dispatched = {}

    async def fake_get(db, document_id):
        return doc

    def fake_dispatch(document_id, file_type="word"):
        dispatched["args"] = (str(document_id), file_type)
        return True

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    monkeypatch.setattr(admin_module, "_dispatch_indexing", fake_dispatch)

    resp = client.post(f"/admin/documents/{doc.id}/index")
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] is True
    # 必须把文档类型一并传给调度器：媒体类要投递到独立的 media 队列
    assert dispatched["args"] == (str(doc.id), "markdown")


def test_admin_index_rejects_unreviewed_409(admin_client, monkeypatch):
    """未审核通过：409，且 detail 可读（前端直接展示）。"""
    client, _ = admin_client
    doc = _make_document(review_status="pending", status="pending")

    async def fake_get(db, document_id):
        return doc

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    resp = client.post(f"/admin/documents/{doc.id}/index")
    assert resp.status_code == 409
    assert "审核" in resp.json()["detail"]


def test_admin_index_rejects_running_409(admin_client, monkeypatch):
    """确实在入库中（updated_at 刚刚）：409，避免并发重跑与重复计费。"""
    client, _ = admin_client
    doc = _stale_indexing_doc(minutes=1)

    async def fake_get(db, document_id):
        return doc

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    resp = client.post(f"/admin/documents/{doc.id}/index")
    assert resp.status_code == 409
    assert "入库中" in resp.json()["detail"]


def test_admin_index_allows_stale_indexing(admin_client, monkeypatch):
    """★ 核心回归：入库已僵死必须放行（否则文档永久卡住、只能改库）。

    修复前该场景返回 409，配合前端按钮禁用，等于彻底断掉恢复路径。
    """
    client, _ = admin_client
    doc = _stale_indexing_doc()
    called = {"n": 0}

    async def fake_get(db, document_id):
        return doc

    def fake_dispatch(document_id, file_type="word"):
        called["n"] += 1
        return False  # 模拟 Celery 不可用、回退进程内执行

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    monkeypatch.setattr(admin_module, "_dispatch_indexing", fake_dispatch)

    resp = client.post(f"/admin/documents/{doc.id}/index")
    assert resp.status_code == 200
    assert resp.json()["queued"] is False
    assert called["n"] == 1


def test_admin_index_not_found_returns_404(admin_client, monkeypatch):
    """文档不存在 / 已删除：404。"""
    client, _ = admin_client

    async def fake_get(db, document_id):
        raise DocumentNotFound(document_id)

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    resp = client.post(f"/admin/documents/{uuid4()}/index")
    assert resp.status_code == 404


# ---------------------------------------------------------------------
# 下线：POST /admin/documents/{id}/unpublish
# ---------------------------------------------------------------------


def test_admin_unpublish_ok(admin_client, monkeypatch):
    """正常下线：同步执行，返回 queued=False 与提示文案。"""
    client, _ = admin_client
    doc = _make_document(review_status="approved", status="indexed")
    seen = {}

    async def fake_get(db, document_id):
        return doc

    async def fake_unpublish(document_id):
        seen["id"] = str(document_id)

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    monkeypatch.setattr(admin_module.indexing_service, "unpublish_document", fake_unpublish)

    resp = client.post(f"/admin/documents/{doc.id}/unpublish")
    assert resp.status_code == 200
    assert resp.json()["queued"] is False
    assert seen["id"] == str(doc.id)


def test_admin_unpublish_stale_indexing_ok(admin_client, monkeypatch):
    """★ 回归：僵死文档也必须能下线（否则「重入库」与「下线」被同一状态同时堵死）。"""
    client, _ = admin_client
    doc = _stale_indexing_doc()

    async def fake_get(db, document_id):
        return doc

    async def fake_unpublish(document_id):
        return None

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    monkeypatch.setattr(admin_module.indexing_service, "unpublish_document", fake_unpublish)

    resp = client.post(f"/admin/documents/{doc.id}/unpublish")
    assert resp.status_code == 200


def test_admin_unpublish_running_409(admin_client, monkeypatch):
    """确实在入库中时下线应 409（由服务层状态机拒绝）。"""
    client, _ = admin_client

    async def fake_get(db, document_id):
        return _make_document(review_status="approved", status="indexing")

    async def fake_unpublish(document_id):
        raise IndexingInProgress("文档正在入库中，请入库完成后再下线")

    monkeypatch.setattr(admin_module.review_service, "get_document_for_review", fake_get)
    monkeypatch.setattr(admin_module.indexing_service, "unpublish_document", fake_unpublish)

    resp = client.post(f"/admin/documents/{uuid4()}/unpublish")
    assert resp.status_code == 409
    assert "入库中" in resp.json()["detail"]
