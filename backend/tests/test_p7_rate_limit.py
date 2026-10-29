# P7 LLM 限流与配额测试
# ---------------------------------------------------------------------
# 覆盖《需求清单》P7 功能1「LLM 调用限流/配额」的三个维度与降级行为。
#
# 重要隔离说明：
#   - 强制走**进程内计数**（把 _get_redis 替换为返回 None），
#     否则会写真实 Redis 并影响本机其它测试/开发数据；
#   - 每个用例前清空内存计数，保证用例之间互不影响（这是「偶发失败」的常见根源）。
#
# 只验证行为口径（什么时候允许、什么时候拒绝、拒绝理由是否可读），
# 不验证 Redis 命令细节——那属于集成测试范畴。

from datetime import datetime

import pytest

from app.config import get_settings
from app.core import rate_limit
from app.core.errors import RateLimitExceeded
from app.core.rate_limit import _MemoryQuotaStore

USER = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def memory_backend(monkeypatch):
    """强制内存后端并清空计数（含 Redis 全局状态复位）。"""
    async def _no_redis():
        """模拟「Redis 不可用/未安装」，让限流走内存实现。"""
        return None

    monkeypatch.setattr(rate_limit, "_get_redis", _no_redis)
    # 每个用例一份干净的内存计数，避免用例间串味
    monkeypatch.setattr(rate_limit, "_memory_store", _MemoryQuotaStore())
    monkeypatch.setattr(rate_limit, "_redis_unavailable", False)

    cfg = get_settings()
    # 默认打开限流（conftest 里为了其它测试关掉了）
    monkeypatch.setattr(cfg, "LLM_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(cfg, "LLM_RATE_LIMIT_PER_MINUTE", 3)
    monkeypatch.setattr(cfg, "LLM_RATE_LIMIT_PER_DAY", 10)
    monkeypatch.setattr(cfg, "LLM_DAILY_TOKEN_QUOTA", 1000)
    yield


# ---------------------------------------------------------------------
# 内存计数窗口
# ---------------------------------------------------------------------


def test_memory_store_resets_when_minute_changes():
    """跨分钟应清零分钟计数，日计数保留。"""
    store = _MemoryQuotaStore()
    store.incr_request("u")
    store.incr_request("u")
    assert store.read("u")["minute"] == 2

    # 人为把窗口标记改成上一分钟，模拟时间流逝
    entry = store._data["u"]
    entry["minute"] = "197001010000"
    assert store.read("u")["minute"] == 0
    assert store.read("u")["day"] == 2


def test_memory_store_resets_when_day_changes():
    """跨天应同时清零日次数与 token 用量。"""
    store = _MemoryQuotaStore()
    store.incr_request("u")
    store.incr_tokens("u", 500)
    store._data["u"]["day"] = "19700101"

    usage = store.read("u")
    assert usage["day"] == 0
    assert usage["tokens"] == 0


# ---------------------------------------------------------------------
# 三个维度
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allows_until_minute_limit_then_rejects():
    """前 3 次放行，第 4 次因超过每分钟上限被拒，并给出 Retry-After。"""
    for _ in range(3):
        status = await rate_limit.check_and_consume(USER)
        assert status.allowed is True

    status = await rate_limit.check_and_consume(USER)
    assert status.allowed is False
    assert status.scope == "minute"
    assert status.limit == 3
    # 等待时间必须落在 1~60 秒之间（距离下一分钟）
    assert 1 <= status.retry_after <= 60
    assert "频繁" in status.message


@pytest.mark.asyncio
async def test_rejects_when_daily_limit_reached(monkeypatch):
    """分钟上限放宽后，日上限应独立生效。"""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "LLM_RATE_LIMIT_PER_MINUTE", 100)
    monkeypatch.setattr(cfg, "LLM_RATE_LIMIT_PER_DAY", 2)

    assert (await rate_limit.check_and_consume(USER)).allowed is True
    assert (await rate_limit.check_and_consume(USER)).allowed is True

    status = await rate_limit.check_and_consume(USER)
    assert status.allowed is False
    assert status.scope == "day"
    assert status.limit == 2
    # 日维度等待到次日零点
    assert 1 <= status.retry_after <= 86400


@pytest.mark.asyncio
async def test_rejects_when_token_quota_exhausted():
    """次数未超但 token 已用满时，仍应拒绝（成本保护的核心）。"""
    await rate_limit.record_tokens(USER, 600, 500)  # 合计 1100 > 配额 1000

    status = await rate_limit.check_and_consume(USER)
    assert status.allowed is False
    assert status.scope == "tokens"
    assert status.tokens_used == 1100


@pytest.mark.asyncio
async def test_token_quota_zero_means_unlimited(monkeypatch):
    """配额配成 0 表示不限制，不应拦截。"""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "LLM_DAILY_TOKEN_QUOTA", 0)
    await rate_limit.record_tokens(USER, 999999, 999999)
    status = await rate_limit.check_and_consume(USER)
    assert status.allowed is True


@pytest.mark.asyncio
async def test_disabled_returns_allowed_and_disabled_source(monkeypatch):
    """总开关关闭时应放行，且 source 标记为 disabled（状态页据此展示）。"""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "LLM_RATE_LIMIT_ENABLED", False)

    status = await rate_limit.check_and_consume(USER)
    assert status.allowed is True
    assert status.enabled is False
    assert status.source == "disabled"


# ---------------------------------------------------------------------
# 用量记录与查询
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_tokens_accumulates_and_dedups_negative():
    """token 累加应正确；负数（上游异常值）不计入。"""
    await rate_limit.record_tokens(USER, 100, 50)
    await rate_limit.record_tokens(USER, -10, -5)
    status = await rate_limit.get_quota_status(USER)
    assert status.tokens_used == 150


@pytest.mark.asyncio
async def test_get_quota_status_does_not_consume():
    """查询配额是只读操作：连续查询不应消耗次数。"""
    await rate_limit.get_quota_status(USER)
    await rate_limit.get_quota_status(USER)
    status = await rate_limit.get_quota_status(USER)
    assert status.minute_used == 0
    assert status.allowed is True


@pytest.mark.asyncio
async def test_reset_quota_clears_counters():
    """管理员重置后，次数与 token 都应归零。"""
    await rate_limit.check_and_consume(USER)
    await rate_limit.record_tokens(USER, 100, 100)

    await rate_limit.reset_quota(USER)
    status = await rate_limit.get_quota_status(USER)
    assert status.minute_used == 0
    assert status.day_used == 0
    assert status.tokens_used == 0


# ---------------------------------------------------------------------
# 故障策略（fail-open / fail-closed）
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_open_keeps_service_available_when_redis_down(monkeypatch):
    """默认 fail-open：Redis 故障时降级为内存计数，业务仍可用。"""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "RATE_LIMIT_FAIL_OPEN", True)
    monkeypatch.setattr(rate_limit, "_redis_unavailable", True)

    status = await rate_limit.check_and_consume(USER)
    assert status.allowed is True
    assert status.source == "memory"


@pytest.mark.asyncio
async def test_fail_closed_rejects_when_redis_down(monkeypatch):
    """fail-closed 且 Redis 故障时应拒绝（宁可不可用，也不让成本失控）。"""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "RATE_LIMIT_FAIL_OPEN", False)
    # _get_redis 已被 autouse fixture 换成「返回 None」，再标记为「故障」状态
    monkeypatch.setattr(rate_limit, "_redis_unavailable", True)

    status = await rate_limit.check_and_consume(USER)
    assert status.allowed is False
    assert status.scope == "unavailable"
    assert "不可用" in status.message


# ---------------------------------------------------------------------
# 辅助逻辑
# ---------------------------------------------------------------------


def test_estimate_tokens_scales_with_length():
    """估算随长度单调增长，且空串返回 0。"""
    assert rate_limit.estimate_tokens("") == 0
    short = rate_limit.estimate_tokens("你好")
    long = rate_limit.estimate_tokens("你好" * 100)
    assert 0 < short < long


def test_seconds_helpers_are_in_range():
    """Retry 秒数必须落在合理区间（避免出现 0 或负数导致客户端疯狂重试）。"""
    assert 1 <= rate_limit._seconds_to_next_minute() <= 60
    assert 1 <= rate_limit._seconds_to_midnight() <= 86400
    # 至少不该是负值
    assert isinstance(datetime.now(), datetime)


def test_exception_carries_retry_metadata():
    """RateLimitExceeded 必须携带 scope/limit/retry_after，供 429 响应头使用。"""
    exc = RateLimitExceeded("太频繁", scope="minute", limit=20, retry_after=42)
    assert exc.scope == "minute"
    assert exc.limit == 20
    assert exc.retry_after == 42
