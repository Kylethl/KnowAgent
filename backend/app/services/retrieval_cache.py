# 检索结果缓存（P4-DB1：高频命中缓存到 Redis）
# ---------------------------------------------------------------------
# 职责：把「同一问题 → 同一批召回切片」的映射缓存起来，避免重复的
#       embedding 调用与向量检索；知识库高频提问（如「支持哪些格式」）命中率通常很高。
#
# 对应《需求清单》P4 功能1 数据存储项：「pgvector 向量查询；高频命中可缓存到 Redis」。
#
# 设计要点：
#   1. **两级后端，自动降级**：
#      - 首选 Redis（redis.asyncio，异步客户端，不阻塞事件循环）；
#      - Redis 不可达 / 未安装 / 报错时，自动回退到**进程内 TTL + LRU 字典**，
#        保证本地开发（无 Redis）与生产（有 Redis）行为一致、代码零改动。
#   2. **失败绝不影响主链路**：缓存是纯优化项，所有 Redis 异常都被吞掉并记录，
#      调用方永远只看到「命中 / 未命中」两种结果。
#   3. **不可达后退避**：连续失败会置一个短暂的禁用窗口，
#      避免每次提问都花时间重连一个明显挂掉的服务（拖慢首字节时间）。
#   4. **键与值均为纯文本**：键为 sha256 摘要（问题可能很长且含特殊字符），
#      值为 JSON 数组（切片字典列表），便于跨语言排查与手工清理。

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from ..config import get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)

# Redis 连接/读写超时（秒）：缓存只值「快」，宁可放弃也不能拖慢对话首字节
_REDIS_TIMEOUT = 0.5
# Redis 失败后的退避窗口（秒）：窗口内直接走内存缓存，不再尝试重连
_REDIS_RETRY_COOLDOWN = 60.0


def build_cache_key(query: str, top_k: int, threshold: float) -> str:
    """构造缓存键。

    键结构：{prefix}:{sha256(query|top_k|threshold)}
      - 用摘要而非原文：问题长度不可控（上限 8000 字），直接做 key 会撑爆 Redis；
      - 把 top_k / threshold 纳入摘要：检索参数调整后不会误用旧结果。

    Args:
        query: 用户提问（原文）。
        top_k: 本次采用的注入条数。
        threshold: 本次采用的相似度阈值。

    Returns:
        完整的缓存键字符串。
    """
    settings = get_settings()
    raw = f"{query.strip()}|{top_k}|{threshold:.4f}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{settings.RAG_CACHE_KEY_PREFIX}:{digest}"


class _MemoryCache:
    """进程内回退缓存：TTL + LRU（容量上限由 RAG_CACHE_MAX_ITEMS 控制）。

    为什么需要它：
        本地开发常常只起 PostgreSQL 不起 Redis，或 Redis 临时抖动；
        若没有回退，缓存层就成了「可用性短板」。内存版虽不跨进程共享，
        但对单副本开发/小规模部署已能显著减少重复向量检索。
    """

    def __init__(self, max_items: int, ttl: int) -> None:
        """初始化内存缓存。

        Args:
            max_items: 最大条目数（超出按 LRU 淘汰最久未使用项）。
            ttl: 条目有效期（秒），≤0 表示不过期。
        """
        self._data: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self._max_items = max(1, max_items)
        self._ttl = ttl

    def get(self, key: str) -> Optional[str]:
        """读取缓存；过期或不存在返回 None。

        Args:
            key: 缓存键。

        Returns:
            缓存值（JSON 字符串）或 None。
        """
        item = self._data.get(key)
        if item is None:
            return None
        expire_at, value = item
        # 惰性过期：读写时判断，无需后台清理线程
        if self._ttl > 0 and expire_at < time.time():
            self._data.pop(key, None)
            return None
        # 命中后移到队尾，维持 LRU 语义
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: str) -> None:
        """写入缓存（已存在则覆盖并刷新过期时间）。

        Args:
            key: 缓存键。
            value: 缓存值（JSON 字符串）。
        """
        expire_at = time.time() + self._ttl if self._ttl > 0 else float("inf")
        self._data[key] = (expire_at, value)
        self._data.move_to_end(key)
        # 超容量：淘汰最久未使用的条目（队首）
        while len(self._data) > self._max_items:
            self._data.popitem(last=False)

    def clear(self) -> None:
        """清空全部条目（测试与手动失效用）。"""
        self._data.clear()

    def __len__(self) -> int:
        """当前条目数（含尚未惰性清理的过期项），仅供观测。"""
        return len(self._data)


class RetrievalCache:
    """检索缓存门面：优先 Redis，失败自动退回内存。"""

    def __init__(self, force_memory: bool = False) -> None:
        """初始化缓存门面。

        Args:
            force_memory: 强制只用内存后端（单测用来隔离 Redis 环境差异）。
        """
        settings = get_settings()
        self._ttl = settings.RAG_CACHE_TTL
        self._enabled = settings.RAG_CACHE_ENABLED
        self._memory = _MemoryCache(settings.RAG_CACHE_MAX_ITEMS, settings.RAG_CACHE_TTL)
        self._redis = None  # 懒初始化：首次访问时才建立连接
        self._redis_dead_until = 0.0  # Redis 退避截止时间戳（0 表示未退避）
        self._force_memory = force_memory
        # 后端标记：redis（当前在用 Redis）/ memory（已回退）/ unknown（尚未探测）
        self._backend = "memory" if force_memory else "unknown"

    @property
    def backend(self) -> str:
        """当前实际生效的缓存后端（redis / memory / unknown），用于日志与健康排查。"""
        return self._backend

    async def _get_redis(self):
        """懒加载并返回 Redis 异步客户端；不可用时返回 None。

        Returns:
            redis.asyncio.Redis 实例，或 None（未启用 / 未安装 / 处于退避窗口）。
        """
        if self._force_memory:
            return None
        # 退避窗口内不再尝试重连，直接走内存
        if self._redis_dead_until > time.time():
            return None
        if self._redis is not None:
            return self._redis

        settings = get_settings()
        try:
            # 懒导入：未安装 redis 包时不影响其它功能
            from redis.asyncio import Redis  # type: ignore[import-not-found]

            self._redis = Redis.from_url(
                settings.REDIS_CACHE_URL,
                decode_responses=True,
                socket_timeout=_REDIS_TIMEOUT,
                socket_connect_timeout=_REDIS_TIMEOUT,
            )
            # 立即探活：避免把「首个请求才发现连不上」的延迟摊到对话上
            await self._redis.ping()
            self._backend = "redis"
            logger.info("检索缓存已启用 Redis：%s", settings.REDIS_CACHE_URL)
            return self._redis
        except Exception as exc:  # noqa: BLE001 —— 未安装/不可达/鉴权失败统一回退
            self._mark_redis_dead(exc)
            return None

    def _mark_redis_dead(self, exc: Exception) -> None:
        """标记 Redis 不可用并进入退避窗口（后续请求直接走内存缓存）。

        Args:
            exc: 触发降级的异常（仅记录，不向上抛）。
        """
        self._redis = None
        self._redis_dead_until = time.time() + _REDIS_RETRY_COOLDOWN
        self._backend = "memory"
        logger.warning(
            "检索缓存回退为进程内缓存（Redis 不可用：%s）；%.0fs 内不再重试",
            exc, _REDIS_RETRY_COOLDOWN,
        )

    async def get(self, key: str) -> Optional[List[Dict[str, Any]]]:
        """读取缓存的检索结果。

        Args:
            key: 缓存键（见 build_cache_key）。

        Returns:
            切片字典列表；未命中 / 未启用 / 解析失败均返回 None（调用方视为未命中）。
        """
        if not self._enabled:
            return None

        # 1) 先查 Redis（可用时）
        client = await self._get_redis()
        if client is not None:
            try:
                raw = await client.get(key)
                if raw is None:
                    return None
                return json.loads(raw)
            except Exception as exc:  # noqa: BLE001 —— 读取失败按未命中处理
                self._mark_redis_dead(exc)

        # 2) 回退内存
        raw = self._memory.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            # 内存中的脏数据（理论上不会出现）：当作未命中
            return None

    async def set(self, key: str, items: List[Dict[str, Any]]) -> None:
        """写入检索结果缓存。

        Args:
            key: 缓存键。
            items: 切片字典列表（可为空列表，表示「确实没有命中」，即负缓存）。
        """
        if not self._enabled:
            return

        try:
            payload = json.dumps(items, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            # 序列化失败（如混入非基本类型）只记录，不影响主链路
            logger.warning("检索结果无法序列化，跳过缓存：%s", exc)
            return

        # 1) 写 Redis（可用时）
        client = await self._get_redis()
        if client is not None:
            try:
                await client.set(key, payload, ex=max(1, self._ttl))
                return
            except Exception as exc:  # noqa: BLE001 —— 写失败则降级到内存
                self._mark_redis_dead(exc)

        # 2) 写内存
        self._memory.set(key, payload)

    async def clear(self) -> None:
        """仅清空进程内缓存（不触碰 Redis）。

        说明：需要「按前缀整体失效」时用 invalidate()；
        本方法用于测试或只想丢弃本地副本的场景。
        """
        self._memory.clear()

    async def invalidate(self) -> int:
        """按前缀失效全部检索缓存（Redis SCAN + 批量 DEL，内存直接清空）。

        为什么需要它：
            检索缓存默认靠 TTL 自然过期（RAG_CACHE_TTL 秒），
            但「文档重入库 / 下线」属于知识库内容变更——若继续用旧召回结果，
            会出现「文档已下线却仍被引用」这种明显错误。
            因此入库流程结束时调用本方法，让新内容立即可检索、下线内容立即消失。

        Returns:
            删除的缓存键数量（内存后端返回清空前的条目数；Redis 不可用时为 0）。
        """
        removed = len(self._memory)
        self._memory.clear()

        client = await self._get_redis()
        if client is None:
            return removed

        settings = get_settings()
        pattern = f"{settings.RAG_CACHE_KEY_PREFIX}:*"
        try:
            batch: List[str] = []
            # SCAN 而非 KEYS：避免在大 keyspace 上阻塞 Redis
            async for key in client.scan_iter(match=pattern, count=200):
                batch.append(key)
                if len(batch) >= 200:
                    removed += await client.delete(*batch)
                    batch.clear()
            if batch:
                removed += await client.delete(*batch)
            logger.info("已按前缀失效检索缓存：pattern=%s，删除 %d 条", pattern, removed)
        except Exception as exc:  # noqa: BLE001 —— 失效失败只影响新鲜度，不影响可用性
            self._mark_redis_dead(exc)
        return removed


# 模块级单例：Redis 客户端与内存字典都应跨请求复用
_cache_singleton: Optional[RetrievalCache] = None


def get_retrieval_cache() -> RetrievalCache:
    """获取（并缓存）检索缓存单例。

    Returns:
        进程内唯一的 RetrievalCache 实例。
    """
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = RetrievalCache()
    return _cache_singleton


def reset_retrieval_cache(force_memory: bool = False) -> RetrievalCache:
    """重建缓存单例（测试用；也可在运行期手动失效）。

    Args:
        force_memory: 新实例是否强制使用内存后端。

    Returns:
        新建的 RetrievalCache 实例。
    """
    global _cache_singleton
    _cache_singleton = RetrievalCache(force_memory=force_memory)
    return _cache_singleton
