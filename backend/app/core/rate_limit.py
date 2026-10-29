# LLM 调用限流与配额（P7-B1）
# ---------------------------------------------------------------------
# 对应《需求清单》P7 功能1「后端：LLM 调用限流/配额」。
#
# 为什么需要它（真实风险，不是形式要求）：
#   1. 成本风险：deepseek 按 token 计费，一个脚本循环提问就能把预算打光；
#   2. 可用性风险：单用户刷接口会占满上游并发，其他用户全部排队；
#   3. 滥用风险：公开站点没有配额，等于把 API Key 借给全互联网用。
#
# 三个维度（互相独立，任一超限即拒绝）：
#   - minute：单用户每分钟对话次数 → 挡住脚本连点/爬虫；
#   - day   ：单用户每日对话次数 → 挡住「慢慢刷」的长期滥用；
#   - tokens：单用户每日 token 用量 → 挡住「少次但超长」的成本放大。
#     次数配额挡不住「一次塞 8000 字 + 长回答」，所以 token 维度必须单独存在。
#
# 后端选择（与检索缓存同一套降级哲学）：
#   首选 Redis（跨副本共享，多实例部署下配额才准确）；
#   Redis 不可达/未安装时回退进程内计数（单副本可用；多副本会各自计算，
#   属于可接受的降级——限流是保护措施，不应在依赖故障时把整个问答打死）。
#
# 并发语义（明确的取舍）：
#   采用「先读后写」而非 Lua 原子脚本：极端并发下可能放过多 1~2 次请求。
#   对「防滥用」这个目标来说完全够用，而实现简单、可读、易排障；
#   真要做到严格计数，需要引入 Lua/事务，收益远小于复杂度。
#
# 观测：每次拒绝都会打指标 knowagent_llm_rate_limited_total{scope}，
#      并在 /chat/quota 上暴露给前端，让用户在提问前就知道还剩多少。

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Union
from uuid import UUID

from ..config import get_settings
from ..core import metrics
from ..core.logging import get_logger

logger = get_logger(__name__)

# Redis 操作超时（秒）：限流判断发生在「用户等待回答」的最前面，
# 宁可放弃精确计数也不能让首字节再晚 1 秒。
_REDIS_TIMEOUT = 0.5
# Redis 失败后的退避窗口（秒）：窗口内直接用内存计数，避免每请求都重连
_REDIS_RETRY_COOLDOWN = 60.0


@dataclass
class QuotaStatus:
    """一次配额判定的完整结果（既是内部返回值，也是 /chat/quota 的响应体）。

    Attributes:
        enabled: 是否启用限流（关闭时其余字段只做展示）
        allowed: 本次是否放行
        scope: 被哪个维度拦下（minute / day / tokens）；放行时为空串
        message: 给用户看的中文说明（放行时为空串）
        retry_after: 建议等待秒数（写入 HTTP Retry-After）
        minute_used: 本分钟已用次数
        minute_limit: 每分钟上限
        day_used: 今日已用次数
        day_limit: 每日上限
        tokens_used: 今日已用 token
        tokens_quota: 每日 token 配额（0=不限）
        source: 计数来源——redis / memory / disabled
    """

    enabled: bool = True
    allowed: bool = True
    scope: str = ""
    message: str = ""
    retry_after: int = 0
    # 被触发维度的上限值（放行时为 0）；与 minute_limit/day_limit 的区别：
    # 后者是配置值，limit 是「本次拦下你的那个维度」的上限，便于前端直接展示。
    limit: int = 0
    minute_used: int = 0
    minute_limit: int = 0
    day_used: int = 0
    day_limit: int = 0
    tokens_used: int = 0
    tokens_quota: int = 0
    source: str = "memory"
    # 供日志/指标使用的附加说明（如 Redis 故障原因），不对外暴露
    extra: Dict[str, Any] = field(default_factory=dict)


class _MemoryQuotaStore:
    """进程内配额计数（Redis 不可用时的回退实现）。

    结构：{user_key: {"minute": "202610281530", "m_count": 3,
                      "day": "20261028", "d_count": 12, "tokens": 3400}}
    按「分钟串 / 日期串」判断窗口是否翻篇，翻篇即清零——
    不需要定时清理任务，天然自愈。
    """

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _minute_bucket(now: Optional[datetime] = None) -> str:
        """当前分钟标识（YYYYMMDDHHMM）。"""
        now = now or datetime.now()
        return now.strftime("%Y%m%d%H%M")

    @staticmethod
    def _day_bucket(now: Optional[datetime] = None) -> str:
        """当前日期标识（YYYYMMDD）。"""
        now = now or datetime.now()
        return now.strftime("%Y%m%d")

    def _entry(self, key: str) -> Dict[str, Any]:
        """取出（必要时初始化/翻篇）某用户的计数条目。"""
        minute, day = self._minute_bucket(), self._day_bucket()
        entry = self._data.setdefault(
            key, {"minute": minute, "m_count": 0, "day": day, "d_count": 0, "tokens": 0}
        )
        if entry["minute"] != minute:
            entry["minute"] = minute
            entry["m_count"] = 0
        if entry["day"] != day:
            entry["day"] = day
            entry["d_count"] = 0
            entry["tokens"] = 0
        return entry

    def read(self, key: str) -> Dict[str, int]:
        """读取当前窗口的用量（不产生计数）。"""
        entry = self._entry(key)
        return {
            "minute": int(entry["m_count"]),
            "day": int(entry["d_count"]),
            "tokens": int(entry["tokens"]),
        }

    def incr_request(self, key: str) -> Dict[str, int]:
        """对话次数 +1（同时累加分钟与日两个维度）。"""
        entry = self._entry(key)
        entry["m_count"] += 1
        entry["d_count"] += 1
        return {
            "minute": int(entry["m_count"]),
            "day": int(entry["d_count"]),
            "tokens": int(entry["tokens"]),
        }

    def incr_tokens(self, key: str, tokens: int) -> None:
        """token 用量累加。"""
        if tokens <= 0:
            return
        self._entry(key)["tokens"] += int(tokens)

    def reset(self, key: str) -> None:
        """清空某用户计数（管理/测试用）。"""
        self._data.pop(key, None)


# 全局内存计数实例（模块级单例，进程内共享）
_memory_store = _MemoryQuotaStore()
# Redis 客户端与退避状态（懒初始化）
_redis_client = None
_redis_dead_until = 0.0
# 是否发生过「Redis 本应可用但连接失败」（用于区分「没装/没配」与「故障」）
# 区别很关键：
#   - 没装 redis 包 / 单副本开发：走内存计数即可，属于设计内降级；
#   - 生产配了 Redis 却连不上：属于故障，是否放行取决于 RATE_LIMIT_FAIL_OPEN。
_redis_unavailable = False


def _user_key(user_id: Union[str, UUID]) -> str:
    """把用户标识规范化为字符串（UUID 与 str 统一处理）。"""
    return str(user_id)


def _redis_keys(user_id: Union[str, UUID]) -> Dict[str, str]:
    """构造三个维度的 Redis key（前缀 + 用户 + 时间窗口）。

    Args:
        user_id: 用户标识。

    Returns:
        {"minute": ..., "day": ..., "tokens": ...}
    """
    settings = get_settings()
    prefix = settings.LLM_RATE_LIMIT_KEY_PREFIX
    now = datetime.now()
    uid = _user_key(user_id)
    day = now.strftime("%Y%m%d")
    return {
        "minute": f"{prefix}:m:{uid}:{now.strftime('%Y%m%d%H%M')}",
        "day": f"{prefix}:d:{uid}:{day}",
        "tokens": f"{prefix}:t:{uid}:{day}",
    }


async def _get_redis():
    """获取（并缓存）限流用的 Redis 客户端。

    Returns:
        redis.asyncio.Redis 实例；未启用/未安装/处于退避窗口时返回 None。
    """
    global _redis_client, _redis_dead_until, _redis_unavailable

    settings = get_settings()
    # 限流关闭时不需要 Redis
    if not settings.LLM_RATE_LIMIT_ENABLED:
        return None
    # 退避窗口内直接放弃（避免每请求都尝试重连一个挂掉的实例）
    if _redis_dead_until > time.time():
        return None
    if _redis_client is not None:
        return _redis_client

    try:
        from redis.asyncio import Redis  # 局部导入：未安装时走内存回退

        client = Redis.from_url(
            settings.rate_limit_redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=_REDIS_TIMEOUT,
            socket_timeout=_REDIS_TIMEOUT,
        )
        # 主动 ping 一次：把「连不上」的问题在第一次就暴露并进入退避
        await client.ping()
        _redis_client = client
        # 连接成功后清除「不可用」标记，避免故障恢复后仍被 fail-closed 拒绝
        _redis_unavailable = False
        return client
    except Exception as exc:  # noqa: BLE001 —— 任何 Redis 问题都降级到内存
        _mark_redis_dead(exc)
        return None


def _mark_redis_dead(exc: Exception) -> None:
    """标记 Redis 不可用并进入退避窗口。"""
    global _redis_client, _redis_dead_until, _redis_unavailable

    _redis_client = None
    _redis_dead_until = time.time() + _REDIS_RETRY_COOLDOWN
    _redis_unavailable = True
    logger.warning("限流 Redis 不可用，已降级为进程内计数（%.0f 秒后重试）：%s",
                   _REDIS_RETRY_COOLDOWN, exc)


def _seconds_to_next_minute() -> int:
    """距离下一分钟还有多少秒（分钟维度被拒时的 Retry-After）。"""
    now = datetime.now()
    return max(1, 60 - now.second)


def _seconds_to_midnight() -> int:
    """距离次日零点还有多少秒（日维度被拒时的 Retry-After）。"""
    now = datetime.now()
    elapsed = now.hour * 3600 + now.minute * 60 + now.second
    return max(1, 86400 - elapsed)


def _build_status(
    *,
    enabled: bool,
    used: Dict[str, int],
    source: str,
) -> QuotaStatus:
    """根据当前用量与配置，判定是否放行并组装返回结构。

    判定顺序：分钟 → 每日次数 → 每日 token。
    先判分钟的原因：用户最可能撞到的是「连续点击」，提示「请稍候 30 秒」
    比提示「今日额度已用完」更准确，也更不容易引起误解。

    Args:
        enabled: 限流是否启用。
        used: 当前用量 {"minute": n, "day": n, "tokens": n}。
        source: 计数来源（redis/memory/disabled）。

    Returns:
        QuotaStatus（allowed=False 时带 scope/message/retry_after）。
    """
    settings = get_settings()

    # 关闭限流：全部放行，但仍回传配置值供前端展示
    if not enabled:
        return QuotaStatus(
            enabled=False,
            allowed=True,
            minute_used=used["minute"],
            minute_limit=settings.LLM_RATE_LIMIT_PER_MINUTE,
            day_used=used["day"],
            day_limit=settings.LLM_RATE_LIMIT_PER_DAY,
            tokens_used=used["tokens"],
            tokens_quota=settings.LLM_DAILY_TOKEN_QUOTA,
            source="disabled",
        )

    status = QuotaStatus(
        enabled=True,
        allowed=True,
        minute_used=used["minute"],
        minute_limit=settings.LLM_RATE_LIMIT_PER_MINUTE,
        day_used=used["day"],
        day_limit=settings.LLM_RATE_LIMIT_PER_DAY,
        tokens_used=used["tokens"],
        tokens_quota=settings.LLM_DAILY_TOKEN_QUOTA,
        source=source,
    )

    # 1) 每分钟次数
    if settings.LLM_RATE_LIMIT_PER_MINUTE > 0 and used["minute"] >= settings.LLM_RATE_LIMIT_PER_MINUTE:
        status.allowed = False
        status.scope = "minute"
        status.limit = settings.LLM_RATE_LIMIT_PER_MINUTE
        status.retry_after = _seconds_to_next_minute()
        status.message = (
            f"提问过于频繁（上限 {settings.LLM_RATE_LIMIT_PER_MINUTE} 次/分钟），"
            f"请等待 {status.retry_after} 秒后重试。"
        )
        return status

    # 2) 每日次数
    if settings.LLM_RATE_LIMIT_PER_DAY > 0 and used["day"] >= settings.LLM_RATE_LIMIT_PER_DAY:
        status.allowed = False
        status.scope = "day"
        status.limit = settings.LLM_RATE_LIMIT_PER_DAY
        status.retry_after = _seconds_to_midnight()
        status.message = (
            f"今日提问次数已达上限（{settings.LLM_RATE_LIMIT_PER_DAY} 次），"
            "明日 00:00 重置；如需更高额度请联系管理员。"
        )
        return status

    # 3) 每日 token 配额
    quota = settings.LLM_DAILY_TOKEN_QUOTA
    if quota > 0 and used["tokens"] >= quota:
        status.allowed = False
        status.scope = "tokens"
        status.limit = quota
        status.retry_after = _seconds_to_midnight()
        status.message = (
            f"今日用量已达 token 配额（{quota}），明日 00:00 重置；"
            "如需更高额度请联系管理员。"
        )
        return status

    return status


def _unavailable_status() -> QuotaStatus:
    """构造「限流依赖不可用」的拒绝结果（fail-closed 模式使用）。

    只有在 **配置了 Redis 却连不上** 且 RATE_LIMIT_FAIL_OPEN=false 时才会走到这里：
    此时无法准确计数，保守起见拒绝请求，避免成本失控。
    """
    return QuotaStatus(
        enabled=True,
        allowed=False,
        scope="unavailable",
        message="限流服务暂不可用（Redis 连接失败），请稍后重试或联系管理员",
        # 与 Redis 退避窗口一致：告诉客户端多久后再试比较合理
        retry_after=int(_REDIS_RETRY_COOLDOWN),
        source="unavailable",
    )


async def get_quota_status(user_id: Union[str, UUID]) -> QuotaStatus:
    """只读查询当前配额状态（供 /chat/quota 与状态页使用，不产生计数）。

    Args:
        user_id: 用户标识。

    Returns:
        QuotaStatus（allowed 反映「现在还能不能提问」）。
    """
    settings = get_settings()
    if not settings.LLM_RATE_LIMIT_ENABLED:
        return _build_status(enabled=False, used={"minute": 0, "day": 0, "tokens": 0}, source="disabled")

    keys = _redis_keys(user_id)
    client = await _get_redis()
    if client is None and _redis_unavailable and not settings.RATE_LIMIT_FAIL_OPEN:
        # fail-closed：配了 Redis 但连不上 → 无法准确计数，直接拒绝
        return _unavailable_status()
    if client is not None:
        try:
            # mget 一次取三个值，减少往返
            values = await client.mget(keys["minute"], keys["day"], keys["tokens"])
            used = {
                "minute": int(values[0] or 0),
                "day": int(values[1] or 0),
                "tokens": int(values[2] or 0),
            }
            return _build_status(enabled=True, used=used, source="redis")
        except Exception as exc:  # noqa: BLE001
            _mark_redis_dead(exc)

    used = _memory_store.read(_user_key(user_id))
    return _build_status(enabled=True, used=used, source="memory")


async def check_and_consume(user_id: Union[str, UUID]) -> QuotaStatus:
    """校验配额并**消耗**一次对话额度（放行时才计数）。

    调用时机：/chat 进入流式生成之前。先判定再计数，
    避免「被拒绝的请求反而延长封禁时间」——用户体验上更合理。

    Args:
        user_id: 用户标识。

    Returns:
        QuotaStatus；allowed=False 时调用方应返回 429。
    """
    settings = get_settings()
    status = await get_quota_status(user_id)
    if not status.enabled or not status.allowed:
        # 被拒绝时打点：区分维度，便于看「用户主要撞哪个限制」
        if status.scope:
            metrics.inc_rate_limited(status.scope)
            logger.warning(
                "LLM 调用被限流：user=%s，scope=%s，used=%s/%s，source=%s",
                _user_key(user_id),
                status.scope,
                status.minute_used if status.scope == "minute" else status.day_used,
                status.limit,
                status.source,
            )
        return status

    keys = _redis_keys(user_id)
    client = await _get_redis()
    if client is not None:
        try:
            # 计数 +1，并确保 key 有 TTL（分钟 key 2 分钟、日 key 25 小时）
            pipe = client.pipeline()
            pipe.incr(keys["minute"])
            pipe.expire(keys["minute"], 120)
            pipe.incr(keys["day"])
            pipe.expire(keys["day"], 90000)
            results = await pipe.execute()
            status.minute_used = int(results[0])
            status.day_used = int(results[2])
            status.source = "redis"
            return status
        except Exception as exc:  # noqa: BLE001
            _mark_redis_dead(exc)

    used = _memory_store.incr_request(_user_key(user_id))
    status.minute_used = used["minute"]
    status.day_used = used["day"]
    status.source = "memory"
    return status


async def record_tokens(user_id: Union[str, UUID], prompt_tokens: int, completion_tokens: int) -> int:
    """记录一次调用的 token 用量（回答结束后调用）。

    Args:
        user_id: 用户标识。
        prompt_tokens: 输入 token 数（估算或上游返回）。
        completion_tokens: 输出 token 数。

    Returns:
        本次计入的 token 总数（便于日志与指标复用）。
    """
    total = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
    if total <= 0:
        return 0

    settings = get_settings()
    if not settings.LLM_RATE_LIMIT_ENABLED:
        # 限流关闭时不必写计数，但指标仍要记（成本看板需要）
        metrics.inc_llm_tokens(prompt_tokens, completion_tokens)
        return total

    keys = _redis_keys(user_id)
    client = await _get_redis()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.incrby(keys["tokens"], total)
            pipe.expire(keys["tokens"], 90000)
            await pipe.execute()
            metrics.inc_llm_tokens(prompt_tokens, completion_tokens)
            return total
        except Exception as exc:  # noqa: BLE001
            _mark_redis_dead(exc)

    _memory_store.incr_tokens(_user_key(user_id), total)
    metrics.inc_llm_tokens(prompt_tokens, completion_tokens)
    return total


async def reset_quota(user_id: Union[str, UUID]) -> None:
    """清空某用户的配额计数（管理员临时解封 / 单测使用）。

    Args:
        user_id: 用户标识。
    """
    keys = _redis_keys(user_id)
    client = await _get_redis()
    if client is not None:
        try:
            await client.delete(keys["minute"], keys["day"], keys["tokens"])
        except Exception as exc:  # noqa: BLE001
            _mark_redis_dead(exc)
    _memory_store.reset(_user_key(user_id))


def estimate_tokens(text: str) -> int:
    """粗估一段文本的 token 数（上游未返回 usage 时的兜底）。

    估算口径：中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token。
    这里统一用「字符数 / 1.5」的保守估计——宁可高估（配额更早触发），
    也不要低估导致成本失控。

    Args:
        text: 文本内容。

    Returns:
        估算的 token 数（至少为 0）。
    """
    if not text:
        return 0
    return int(len(text) / 1.5) + 1
