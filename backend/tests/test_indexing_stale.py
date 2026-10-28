# 入库僵死自愈单元测试（P5 修复）
# ---------------------------------------------------------------------
# 背景（真实线上问题）：
#   用户 u_4ff45c 提交的《部署手册.md》状态停留在 indexing，
#   管理员后台只能「预览」，「重新入库」按钮是灰的，文档永久无法入库。
#   根因：① 前端把 status==='indexing' 一律禁用按钮，未区分「真在跑」与「已中断」；
#        ② 后端虽有超时判定，但未把它下发给前端，前端无从判断。
#
# 本文件覆盖修复后的核心逻辑：
#   1. stale_minutes_for：文本 10 分钟 / 媒体 60 分钟（媒体 OCR/ASR 更慢）
#   2. indexing_elapsed_seconds：仅 indexing 有值
#   3. is_indexing_stale：真在跑为 False，超时为 True，缺时间戳按僵死处理
#   4. assert_can_index：僵死放行、真在跑拒绝（含媒体按类型放宽阈值的用例）
#   5. _mark_indexing：并发抢占语义（rowcount 决定成败）+ SQL 带状态守卫
#   6. AdminDocumentOut：新增字段可被序列化，保证前端拿得到
#
# 说明：真正的「重跑管线」涉及真实 DB / 存储 / 模型，交由真实环境端到端验证。

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core.errors import IndexingInProgress, IndexingNotAllowed
from app.db.models import Document
from app.schemas import AdminDocumentOut
from app.services import indexing_service as isvc


def _make_doc(
    status: str = "indexing",
    review_status: str = "approved",
    file_type: str = "markdown",
    idle_minutes: float | None = 0,
) -> Document:
    """构造一个仅用于状态判定的 Document（不写库）。

    Args:
        status: 生命周期状态。
        review_status: 审核结论。
        file_type: 文档类型（决定僵死阈值）。
        idle_minutes: updated_at 距现在多少分钟；None 表示 updated_at 为空。

    Returns:
        Document 实例（未加入会话）。
    """
    doc = Document(
        id=uuid4(),
        status=status,
        review_status=review_status,
        file_type=file_type,
    )
    doc.updated_at = (
        None
        if idle_minutes is None
        else datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)
    )
    return doc


# =====================================================================
# 一、阈值与时长
# =====================================================================
def test_stale_minutes_by_file_type():
    """文本类用 10 分钟，媒体类放宽到 60 分钟（长视频 ASR 可能跑很久）。"""
    assert isvc.stale_minutes_for(_make_doc(file_type="markdown")) == 10
    assert isvc.stale_minutes_for(_make_doc(file_type="word")) == 10
    assert isvc.stale_minutes_for(_make_doc(file_type="video")) == 60
    assert isvc.stale_minutes_for(_make_doc(file_type="audio")) == 60
    assert isvc.stale_minutes_for(_make_doc(file_type="image")) == 60


def test_elapsed_seconds_only_for_indexing():
    """只有 indexing 状态才返回时长，其余状态为 None（前端据此决定是否展示卡住时长）。"""
    assert isvc.indexing_elapsed_seconds(_make_doc(status="indexed", idle_minutes=5)) is None
    assert isvc.indexing_elapsed_seconds(_make_doc(status="approved", idle_minutes=5)) is None
    elapsed = isvc.indexing_elapsed_seconds(_make_doc(idle_minutes=5))
    # 允许少量执行耗时误差（5 分钟 ± 5 秒）
    assert elapsed is not None and 295 <= elapsed <= 305


def test_elapsed_seconds_missing_updated_at():
    """updated_at 缺失（历史数据）时返回 0，而不是抛异常。"""
    assert isvc.indexing_elapsed_seconds(_make_doc(idle_minutes=None)) == 0


# =====================================================================
# 二、僵死判定
# =====================================================================
def test_not_stale_when_not_indexing():
    """非入库中状态永远不算僵死（避免误标「入库卡住」）。"""
    for status in ("pending", "approved", "rejected", "indexed"):
        assert isvc.is_indexing_stale(_make_doc(status=status, idle_minutes=999)) is False


def test_fresh_indexing_not_stale():
    """刚刚进入入库中：视为正常执行，不应放开重跑（防并发重复计费）。"""
    assert isvc.is_indexing_stale(_make_doc(idle_minutes=0)) is False
    assert isvc.is_indexing_stale(_make_doc(idle_minutes=5)) is False


def test_stale_indexing_detected():
    """超过 10 分钟仍停留在入库中：判定为僵死（对应线上那条 15 小时的记录）。"""
    assert isvc.is_indexing_stale(_make_doc(idle_minutes=11)) is True
    # 线上实际情形：停留约 950 分钟
    assert isvc.is_indexing_stale(_make_doc(idle_minutes=950)) is True


def test_media_threshold_is_more_lenient():
    """媒体类 30 分钟仍在入库中属正常（OCR/ASR 慢），不能误判为僵死。"""
    assert isvc.is_indexing_stale(_make_doc(file_type="video", idle_minutes=30)) is False
    assert isvc.is_indexing_stale(_make_doc(file_type="video", idle_minutes=61)) is True


def test_stale_when_updated_at_missing():
    """updated_at 为空时宁可允许重跑，也不要让文档永久卡死。"""
    assert isvc.is_indexing_stale(_make_doc(idle_minutes=None)) is True


# =====================================================================
# 三、入库前置校验（API 层与管线共用）
# =====================================================================
def test_assert_can_index_allows_stale():
    """核心修复：僵死文档必须能重新入库（否则管理员无路可走）。"""
    isvc.assert_can_index(_make_doc(idle_minutes=950))  # 不抛异常即通过


def test_assert_can_index_rejects_running():
    """确实在跑时仍拒绝，避免并发重跑覆盖切片。"""
    with pytest.raises(IndexingInProgress):
        isvc.assert_can_index(_make_doc(idle_minutes=1))


def test_assert_can_index_rejects_unreviewed():
    """未审核通过的文档不允许入库，且错误信息需可读。"""
    with pytest.raises(IndexingNotAllowed) as exc:
        isvc.assert_can_index(_make_doc(status="pending", review_status="pending", idle_minutes=0))
    assert "pending" in str(exc.value) or "审核" in str(exc.value)


# =====================================================================
# 四、并发抢占（_mark_indexing）
# =====================================================================
class _FakeResult:
    """模拟 SQLAlchemy 的 CursorResult，只暴露 rowcount。"""

    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _FakeSession:
    """记录执行的语句，并按预设 rowcount 返回，用于验证抢占语义。"""

    def __init__(self, rowcount: int):
        self.rowcount = rowcount
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self.rowcount)


@pytest.mark.asyncio
async def test_mark_indexing_returns_true_when_acquired():
    """rowcount=1 表示抢到执行权，本次调用应继续跑管线。"""
    db = _FakeSession(rowcount=1)
    assert await isvc._mark_indexing(db, _make_doc(idle_minutes=950)) is True


@pytest.mark.asyncio
async def test_mark_indexing_returns_false_when_taken():
    """rowcount=0 表示已被其他任务占住（条件未命中），应放弃本次执行。"""
    db = _FakeSession(rowcount=0)
    assert await isvc._mark_indexing(db, _make_doc(idle_minutes=0)) is False


@pytest.mark.asyncio
async def test_mark_indexing_sql_guards_status():
    """抢占必须是「带条件的 UPDATE」，而不是无条件置为 indexing。

    若退化成无条件 UPDATE，并发请求会各自跑一遍管线（重复计费、互相覆盖切片）。
    """
    db = _FakeSession(rowcount=1)
    await isvc._mark_indexing(db, _make_doc(idle_minutes=950))
    sql = str(db.statements[0])
    assert sql.startswith("UPDATE documents")
    # 必须同时包含「按 id 定位」「状态守卫」「时间守卫」三类条件
    assert "documents.id = " in sql
    assert "documents.status != " in sql
    assert "documents.updated_at" in sql


# =====================================================================
# 五、下发契约（前端据此放开按钮）
# =====================================================================
def test_admin_document_out_has_stale_fields():
    """响应模型必须带 indexing_stale / indexing_elapsed_seconds，否则前端拿不到判定结果。"""
    fields = AdminDocumentOut.model_fields
    assert "indexing_stale" in fields
    assert "indexing_elapsed_seconds" in fields
    # 默认值要安全：未入库中的文档不应被标成卡住
    assert fields["indexing_stale"].default is False
    assert fields["indexing_elapsed_seconds"].default is None


def test_to_out_fills_stale_flag():
    """_to_out 必须真实计算僵死标记（而不是留空），否则按钮仍然是灰的。"""
    from app.api.admin import _to_out

    doc = _make_doc(idle_minutes=950)
    # 补齐序列化所需的其余字段
    doc.id = None
    doc.filename = "部署手册.md"
    doc.chunk_count = 3
    doc.size_bytes = 1024
    doc.created_at = datetime.now(timezone.utc)
    doc.indexed_at = None
    doc.review_note = None
    doc.reviewed_at = None
    doc.reviewer_id = None
    doc.user_id = None
    doc.embedding_model = None
    doc.extract_meta = None

    out = _to_out(doc, {})
    assert out.indexing_stale is True
    assert out.indexing_elapsed_seconds is not None
