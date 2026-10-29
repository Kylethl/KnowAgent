# P7 审计日志测试
# ---------------------------------------------------------------------
# 覆盖《需求清单》P7 功能2「审计日志」的写入、脱敏、查询与展示口径。
#
# 隔离策略：
#   - conftest 把 AUDIT_ENABLED 设为 false，因此默认不会写真实数据库；
#   - 需要验证「落库内容」的用例里，用假 SessionLocal 捕获 AuditLog 行对象，
#     既能断字段，又不产生副作用数据（这是审计测试最稳妥的做法）。

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.config import get_settings
from app.core import request_context
from app.db.models import AuditLog, User
from app.services import audit_service


# ---------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------


class _FakeSessionCtx:
    """模拟 `async with SessionLocal() as session`，记录 add/commit 行为。"""

    def __init__(self, sink: list, fail: bool = False):
        self._sink = sink
        self._fail = fail
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, row):
        """收集待写入行，便于断言字段。"""
        self._sink.append(row)

    async def commit(self):
        """模拟提交；fail=True 时抛异常以验证「审计失败不影响业务」。"""
        if self._fail:
            raise RuntimeError("数据库挂了")
        self.committed = True


def _make_user(username: str = "alice", role: str = "admin") -> User:
    """构造内存用户对象（不落库）。"""
    user = User()
    user.id = uuid4()
    user.username = username
    user.role = role
    return user


@pytest.fixture()
def audit_enabled(monkeypatch):
    """打开审计开关（conftest 默认关闭）。"""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "AUDIT_ENABLED", True)
    return cfg


# ---------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------


def test_redact_masks_sensitive_keys_recursively():
    """password / token / api_key 等键的值必须被抹掉，嵌套结构同样处理。"""
    cleaned = audit_service._redact(
        {
            "username": "alice",
            "password": "p@ssw0rd",
            "nested": {"api_key": "sk-123", "note": "ok"},
            "list": [{"access_token": "abc"}, {"file": "a.md"}],
        }
    )
    assert cleaned["username"] == "alice"
    assert cleaned["password"] == "***"
    assert cleaned["nested"]["api_key"] == "***"
    assert cleaned["nested"]["note"] == "ok"
    assert cleaned["list"][0]["access_token"] == "***"
    assert cleaned["list"][1]["file"] == "a.md"


def test_redact_truncates_long_strings():
    """超长字符串应截断到 500 字符（detail 是 JSONB，存长文无价值且拖慢查询）。"""
    cleaned = audit_service._redact({"note": "x" * 2000})
    assert len(cleaned["note"]) == 500


def test_redact_limits_list_length():
    """列表最多保留 50 项，防止把批量操作明细全量写进审计。"""
    cleaned = audit_service._redact({"ids": list(range(200))})
    assert len(cleaned["ids"]) == 50


def test_redact_converts_unknown_types_to_str():
    """UUID / datetime 等非 JSON 原生类型应转字符串，保证一定能入库。"""
    value = uuid4()
    cleaned = audit_service._redact({"id": value, "at": datetime.now()})
    assert cleaned["id"] == str(value)
    assert isinstance(cleaned["at"], str)


# ---------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_writes_expected_fields(audit_enabled, monkeypatch):
    """落库内容应包含动作、身份快照、来源信息与脱敏后的 detail。"""
    sink: list = []
    monkeypatch.setattr(audit_service, "SessionLocal", lambda: _FakeSessionCtx(sink))
    admin = _make_user()
    request_context.bind_request_context("req-99887766", "203.0.113.9", "pytest-UA")

    await audit_service.record(
        "document.review",
        user=admin,
        resource_type="document",
        resource_id="doc-1",
        detail={"action": "approved", "password": "should-be-masked"},
    )

    assert len(sink) == 1
    row = sink[0]
    assert isinstance(row, AuditLog)
    assert row.action == "document.review"
    assert row.result == "success"
    assert row.username == "alice"
    assert row.role == "admin"
    assert str(row.user_id) == str(admin.id)
    assert row.resource_type == "document"
    assert row.resource_id == "doc-1"
    # 来源信息来自请求上下文（中间件写入）
    assert row.ip == "203.0.113.9"
    assert row.request_id == "req-99887766"
    assert row.detail["password"] == "***"
    request_context.clear_request_context()


@pytest.mark.asyncio
async def test_record_failure_marks_result(audit_enabled, monkeypatch):
    """失败事件应写 result=failure，并把原因放进 detail。"""
    sink: list = []
    monkeypatch.setattr(audit_service, "SessionLocal", lambda: _FakeSessionCtx(sink))

    await audit_service.record_failure("auth.login_failed", reason="invalid_credentials", username="bob")

    row = sink[0]
    assert row.result == "failure"
    assert row.detail["reason"] == "invalid_credentials"
    # 未登录尝试：没有 user_id，但有用户名字符串（便于识别撞库目标）
    assert row.user_id is None
    assert row.username == "bob"


@pytest.mark.asyncio
async def test_record_is_noop_when_disabled(monkeypatch):
    """审计关闭时不应建立数据库会话（连尝试都不该有）。"""
    called = {"n": 0}

    def _explode():
        called["n"] += 1
        raise AssertionError("审计关闭时不应连接数据库")

    monkeypatch.setattr(audit_service, "SessionLocal", _explode)
    cfg = get_settings()
    monkeypatch.setattr(cfg, "AUDIT_ENABLED", False)

    await audit_service.record("document.review", username="alice")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_record_swallows_database_errors(audit_enabled, monkeypatch):
    """审计写库失败必须被吞掉：不能因为「记不下来」就让用户操作失败。"""
    monkeypatch.setattr(
        audit_service, "SessionLocal", lambda: _FakeSessionCtx([], fail=True)
    )
    # 不抛异常即为通过
    await audit_service.record("document.upload", username="alice")


@pytest.mark.asyncio
async def test_record_truncates_overlong_fields(audit_enabled, monkeypatch):
    """超长字段应被截断到列宽，避免「审计写入失败」这种无意义的故障。"""
    sink: list = []
    monkeypatch.setattr(audit_service, "SessionLocal", lambda: _FakeSessionCtx(sink))

    await audit_service.record(
        "x" * 200,
        username="u" * 200,
        resource_type="r" * 100,
        resource_id="i" * 400,
        detail={"note": "y"},
    )
    row = sink[0]
    assert len(row.action) == 64
    assert len(row.username) == 64
    assert len(row.resource_type) == 32
    assert len(row.resource_id) == 128


# ---------------------------------------------------------------------
# 展示与查询
# ---------------------------------------------------------------------


def test_to_payload_maps_action_label():
    """动作代码应映射为中文名；未登记的动作原样返回（不显示空白）。"""
    row = AuditLog()
    row.id = uuid4()
    row.user_id = None
    row.username = "alice"
    row.role = "admin"
    row.action = "document.review"
    row.resource_type = "document"
    row.resource_id = "doc-1"
    row.result = "success"
    row.ip = "1.2.3.4"
    row.request_id = "req-1"
    row.detail = None
    row.created_at = datetime(2026, 10, 28, tzinfo=timezone.utc)

    payload = audit_service.to_payload(row)
    assert payload["action_label"] == "审核文档"
    assert payload["created_at"].startswith("2026-10-28")

    row.action = "custom.unknown"
    assert audit_service.to_payload(row)["action_label"] == "custom.unknown"


def test_list_actions_covers_key_business_events():
    """动作字典必须覆盖认证、文档与运维三类关键事件（合规检查的基础）。"""
    values = {item["value"] for item in audit_service.list_actions()}
    for expected in (
        "auth.login",
        "auth.login_failed",
        "document.upload",
        "document.review",
        "document.reindex",
        "document.unpublish",
        "ops.backup.trigger",
        "ops.audit.query",
    ):
        assert expected in values


@pytest.mark.asyncio
async def test_list_audit_logs_returns_items_and_total():
    """查询应返回 (行列表, 总数)，且把筛选条件带进 SQL。"""
    row = AuditLog()
    row.id = uuid4()
    row.created_at = datetime.now(timezone.utc)
    row.action = "auth.login"
    row.result = "success"

    db = AsyncMock()
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = [row]
    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    rows, total = await audit_service.list_audit_logs(
        db, action="auth.login", result="success", limit=10, offset=0
    )
    assert total == 1
    assert rows == [row]
    # 两次查询：一次 count，一次取数据
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_prune_skips_when_retention_non_positive():
    """保留期 ≤0 表示永久保留，不应执行删除（合规场景的保险）。"""
    db = AsyncMock()
    deleted = await audit_service.prune(db, 0)
    assert deleted == 0
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_build_summary_returns_expected_shape():
    """概览应返回总数/今日/失败数/最近时间/动作分布五类口径。"""
    db = AsyncMock()

    def _result(value=None, rows=None):
        """构造单次查询的 Result 桩。"""
        res = MagicMock()
        res.scalar.return_value = value
        res.all.return_value = rows or []
        return res

    db.execute = AsyncMock(
        side_effect=[
            _result(12),                       # total
            _result(3),                        # today
            _result(1),                        # failures
            _result(datetime(2026, 10, 28, tzinfo=timezone.utc)),  # last_event_at
            _result(rows=[("document.review", 5), ("auth.login", 4)]),  # top_actions
        ]
    )

    summary = await audit_service.build_summary(db)
    assert summary["total"] == 12
    assert summary["today"] == 3
    assert summary["failures"] == 1
    assert summary["last_event_at"].startswith("2026-10-28")
    assert summary["top_actions"][0] == {
        "action": "document.review",
        "label": "审核文档",
        "count": 5,
    }
