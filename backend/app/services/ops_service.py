# 运维状态与合规自检（P7-B1 / P7-B2）
# ---------------------------------------------------------------------
# 对应《需求清单》P7：
#   功能1「可监控」→ 状态聚合（依赖健康、限流配置、备份状态、审计概览）
#   功能2「合规」  → 合规自检清单（数据不出域、审计留存、密钥、暴露面）
#
# 设计目标：让运维**打开一个页面就能判断「现在能不能用、有没有风险」**，
#   而不是先 SSH 上去 tail 日志、docker ps、再 psql 查一遍。
#
# 三条实现约定：
#   1. 所有依赖检查都带**短超时**（1~2 秒）：状态页不能因为某个依赖挂掉而转圈 30 秒；
#   2. 检查失败**不抛异常**，而是把失败原因写进该依赖的 detail（页面能显示「为什么」）；
#   3. 只做只读探测：不写库、不改配置、不发业务请求（不会因为「看一眼状态」产生副作用）。
#
# 与 /health 的分工：
#   /health        轻量存活探针，给 docker/K8s 用，必须毫秒级返回；
#   /health/ready  就绪探针，检查 DB/Redis 是否可用（失败即不应接流量）；
#   /ops/status    管理端详细状态，允许慢一点，信息量最大。

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..core import egress, metrics, rate_limit, sentry
from ..core.logging import current_level, get_logger
from ..db.engine import engine
from ..services import audit_service, backup_service, media_extract

logger = get_logger(__name__)

# 应用版本：与 main.py 的 FastAPI(version=...) 保持一致（改一处要同步）
APP_VERSION = "0.1.0"

# 进程启动时间（模块导入即进程启动，用于计算 uptime）
_STARTED_AT = time.time()

# 依赖检查的单项超时（秒）：任一依赖卡住都不能拖住整页
_CHECK_TIMEOUT = 2.0


def uptime_seconds() -> int:
    """进程已运行秒数（状态页展示「已稳定运行 N 天」）。"""
    return max(0, int(time.time() - _STARTED_AT))


def _service(
    name: str,
    label: str,
    status: str,
    detail: str = "",
    latency_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """构造一条依赖检查结果。

    Args:
        name: 依赖标识（postgres / redis / ...），供前端做图标映射。
        label: 中文名。
        status: ok / degraded / down / disabled。
        detail: 说明（失败原因或版本信息）。
        latency_ms: 探测耗时（毫秒）。

    Returns:
        依赖状态字典。
    """
    return {
        "name": name,
        "label": label,
        "status": status,
        "detail": detail,
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
    }


async def _check_postgres() -> Dict[str, Any]:
    """检查 PostgreSQL 连通性与版本。"""
    started = time.perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            # version() 只需一次查询，顺带确认「连的是 PG 而不是 PgBouncer 之类」
            version = (await conn.execute(text("SELECT version()"))).scalar() or ""
        latency = (time.perf_counter() - started) * 1000
        metrics.set_dependency("postgres", True)
        # 版本串很长，只取前两段（PostgreSQL 16.4 ...）
        short = " ".join(str(version).split()[:2])
        return _service("postgres", "PostgreSQL", "ok", short, latency)
    except Exception as exc:  # noqa: BLE001 —— 检查失败只降级，不抛
        metrics.set_dependency("postgres", False)
        return _service("postgres", "PostgreSQL", "down", f"连接失败：{exc}")


async def _check_redis() -> Dict[str, Any]:
    """检查 Redis 连通性（Celery broker 所用的 db0）。"""
    settings = get_settings()
    started = time.perf_counter()
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(
            settings.CELERY_BROKER_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=_CHECK_TIMEOUT,
            socket_timeout=_CHECK_TIMEOUT,
        )
        pong = await asyncio.wait_for(client.ping(), timeout=_CHECK_TIMEOUT)
        await client.aclose()
        latency = (time.perf_counter() - started) * 1000
        if pong:
            metrics.set_dependency("redis", True)
            return _service("redis", "Redis（队列/缓存）", "ok", "PING 正常", latency)
        metrics.set_dependency("redis", False)
        return _service("redis", "Redis（队列/缓存）", "degraded", "PING 无响应")
    except Exception as exc:  # noqa: BLE001
        metrics.set_dependency("redis", False)
        # Redis 不可用时系统仍可对话（缓存/队列降级），因此标 degraded 而非 down
        return _service("redis", "Redis（队列/缓存）", "degraded", f"不可达：{exc}")


async def _check_storage() -> Dict[str, Any]:
    """检查对象存储可用性（local 目录可写 / MinIO 桶可访问）。

    具体探测逻辑收敛在 storage.probe_backend（读操作 + 写删探针，无副作用），
    这里只负责计时与状态归类。
    """
    settings = get_settings()
    started = time.perf_counter()
    try:
        from ..services import storage as storage_service

        ok, detail = await asyncio.wait_for(
            storage_service.probe_backend(settings), timeout=_CHECK_TIMEOUT
        )
        latency = (time.perf_counter() - started) * 1000
        metrics.set_dependency("object_storage", bool(ok))
        label = "对象存储" if settings.STORAGE_BACKEND.lower() == "minio" else "对象存储（本地）"
        return _service("object_storage", label, "ok" if ok else "down", detail, latency)
    except asyncio.TimeoutError:
        metrics.set_dependency("object_storage", False)
        return _service("object_storage", "对象存储", "down", "探测超时（超过 2 秒）")
    except Exception as exc:  # noqa: BLE001
        metrics.set_dependency("object_storage", False)
        return _service("object_storage", "对象存储", "down", f"不可用：{exc}")


def _check_llm() -> Dict[str, Any]:
    """检查对话模型配置（不发起真实调用，避免状态页产生费用）。"""
    settings = get_settings()
    if not settings.is_deepseek_configured:
        metrics.set_dependency("deepseek", False)
        return _service(
            "deepseek",
            "对话模型",
            "down",
            "未配置 DEEPSEEK_API_KEY（/chat 会返回配置缺失提示）",
        )
    host = egress.extract_host(settings.DEEPSEEK_BASE_URL)
    metrics.set_dependency("deepseek", True)
    return _service(
        "deepseek",
        "对话模型",
        "ok",
        f"{settings.DEEPSEEK_MODEL} @ {host}",
    )


def _check_embedding() -> Dict[str, Any]:
    """检查向量模型配置（未配置时 RAG 不可用，但对话仍可进行）。"""
    settings = get_settings()
    if not settings.is_qwen_configured:
        metrics.set_dependency("embedding", False)
        return _service(
            "embedding",
            "向量模型",
            "degraded",
            "未配置 QWEN_API_KEY：入库走 mock 向量，检索不启用",
        )
    host = egress.extract_host(settings.QWEN_EMBEDDING_BASE_URL)
    metrics.set_dependency("embedding", True)
    return _service(
        "embedding", "向量模型", "ok", f"{settings.QWEN_EMBEDDING_MODEL} @ {host}"
    )


def _check_media() -> Dict[str, Any]:
    """检查多模态解析能力（OCR/ASR + ffmpeg）。"""
    try:
        caps = media_extract.capabilities()
    except Exception as exc:  # noqa: BLE001
        return _service("media", "多模态解析", "degraded", f"能力探测失败：{exc}")

    ocr, asr, ffmpeg = bool(caps["ocr"]), bool(caps["asr"]), bool(caps["ffmpeg"])
    if ocr and asr and ffmpeg:
        return _service("media", "多模态解析", "ok", "OCR / ASR / ffmpeg 均可用")
    if ocr or asr:
        missing = []
        if not ffmpeg:
            missing.append("ffmpeg")
        if not ocr:
            missing.append("OCR")
        if not asr:
            missing.append("ASR")
        return _service(
            "media", "多模态解析", "degraded", f"部分能力不可用：{'、'.join(missing)}"
        )
    return _service(
        "media",
        "多模态解析",
        "disabled",
        "未启用（MEDIA_EXTRACT_ENABLED=false 或未配置多模态 Key）",
    )


async def _check_celery() -> Dict[str, Any]:
    """检查 Celery worker 是否在线（只在 broker 可达时才探测，避免长等待）。"""
    settings = get_settings()
    if not settings.USE_CELERY:
        return _service("celery", "异步任务", "disabled", "USE_CELERY=false（进程内执行）")
    try:
        from ..tasks.celery_app import celery_app

        # control.ping 走广播，超时 1 秒；在无 worker 的环境里返回空列表
        replies = await asyncio.wait_for(
            asyncio.to_thread(celery_app.control.ping, timeout=1.0), timeout=2.0
        )
        if replies:
            return _service(
                "celery", "异步任务", "ok", f"{len(replies)} 个 worker 在线"
            )
        return _service(
            "celery",
            "异步任务",
            "degraded",
            "broker 可达但无 worker 在线（入库任务会排队堆积）",
        )
    except Exception as exc:  # noqa: BLE001
        return _service("celery", "异步任务", "degraded", f"探测失败：{exc}")


def _build_llm_section(quota: Optional[rate_limit.QuotaStatus]) -> Dict[str, Any]:
    """组装 LLM 配置与当前用户配额部分。

    Args:
        quota: 当前请求者的配额快照（可能为 None，如调用方不是具体用户）。

    Returns:
        LLM 状态字典。
    """
    settings = get_settings()
    return {
        "configured": settings.is_deepseek_configured,
        "model": settings.DEEPSEEK_MODEL,
        "rate_limit_enabled": settings.LLM_RATE_LIMIT_ENABLED,
        "rate_limit_per_minute": settings.LLM_RATE_LIMIT_PER_MINUTE,
        "rate_limit_per_day": settings.LLM_RATE_LIMIT_PER_DAY,
        "daily_token_quota": settings.LLM_DAILY_TOKEN_QUOTA,
        "quota": (
            {
                "allowed": quota.allowed,
                "scope": quota.scope,
                "minute_used": quota.minute_used,
                "minute_limit": quota.minute_limit,
                "day_used": quota.day_used,
                "day_limit": quota.day_limit,
                "tokens_used": quota.tokens_used,
                "tokens_quota": quota.tokens_quota,
                "source": quota.source,
            }
            if quota
            else None
        ),
    }


async def collect_status(db: AsyncSession, user_id: Optional[UUID] = None) -> Dict[str, Any]:
    """聚合系统状态（管理端状态页的唯一数据源）。

    Args:
        db: 数据库会话（用于审计汇总，复用请求会话，避免额外连接）。
        user_id: 当前请求者 ID（用于展示其个人配额用量）。

    Returns:
        与 OpsStatusResponse 对应的字典。
    """
    settings = get_settings()

    # 五个依赖并发探测：串行会让状态页在某个依赖超时时明显变慢
    results = await asyncio.gather(
        _check_postgres(),
        _check_redis(),
        _check_storage(),
        _check_celery(),
        return_exceptions=False,
    )
    services: List[Dict[str, Any]] = list(results)
    # 纯配置类检查无需异步，直接追加
    services.append(_check_llm())
    services.append(_check_embedding())
    services.append(_check_media())

    quota = await rate_limit.get_quota_status(user_id) if user_id else None

    # 审计概览：数据库不可用时不要连带整页失败
    try:
        audit_summary = await audit_service.build_summary(db, days=7)
    except Exception as exc:  # noqa: BLE001
        logger.warning("审计概览统计失败（状态页降级展示）：%s", exc)
        audit_summary = {
            "total": 0,
            "today": 0,
            "failures": 0,
            "last_event_at": None,
            "window_days": 7,
            "top_actions": [],
            "error": str(exc),
        }

    residency = egress.residency_report()

    return {
        "app": {
            "name": settings.APP_NAME,
            "version": APP_VERSION,
            "env": settings.APP_ENV,
            "uptime_seconds": uptime_seconds(),
            "log_format": settings.LOG_FORMAT,
            "log_level": logging_level_name(),
            "sentry_enabled": sentry.is_enabled(),
            "sentry_sdk_missing": sentry.is_sdk_missing(),
            "metrics_enabled": settings.METRICS_ENABLED,
            "metrics_backend": metrics.backend_name(),
        },
        "services": services,
        "llm": _build_llm_section(quota),
        "backup": backup_service.backup_status(),
        "audit": audit_summary,
        "residency": {
            "mode": residency["mode"],
            "region": residency["region"],
            "strict": residency["strict"],
            "allowlist": residency["allowlist"],
            "targets": residency["targets"],
        },
        "metrics": {
            "enabled": settings.METRICS_ENABLED,
            "path": settings.METRICS_PATH,
            "token_required": bool(settings.METRICS_TOKEN),
            "grafana_url": settings.GRAFANA_URL,
        },
        "generated_at": datetime.now().astimezone().isoformat(),
    }


def logging_level_name() -> str:
    """把根日志器级别转成名字（状态页展示当前日志级别）。"""
    level = current_level()
    import logging as _logging

    return _logging.getLevelName(level) if level is not None else "UNKNOWN"


async def build_compliance_report() -> Dict[str, Any]:
    """生成合规自检报告（P7 功能2 的可验证落地）。

    检查项设计原则：每一项都能**由程序判定**，不写「请自行确认」这种空话；
    确实无法自动判定的（如 HTTPS 是否已在网关开启）给出 warn 与明确的自查方法。

    Returns:
        {"generated_at": ..., "summary": {"pass": n, "warn": n, "fail": n},
         "items": [{key,title,category,status,detail,hint}]}
    """
    settings = get_settings()
    residency = egress.residency_report()
    items: List[Dict[str, Any]] = []

    def add(key: str, title: str, category: str, status: str, detail: str, hint: str = "") -> None:
        """内部小工具：追加一条检查项（减少重复的字典字面量）。"""
        items.append(
            {
                "key": key,
                "title": title,
                "category": category,
                "status": status,
                "detail": detail,
                "hint": hint,
            }
        )

    # ---------- 数据不出域 ----------
    blocked = [t for t in residency["targets"] if not t["allowed"]]
    if not residency["strict"]:
        add(
            "residency_mode",
            "数据不出域模式",
            "数据合规",
            "warn",
            f"当前为 standard 模式，出站目标仅记录不拦截；区域标识 {residency['region']}",
            "私有化/等保场景请设 DATA_RESIDENCY_MODE=strict 并配置 OUTBOUND_ALLOWED_HOSTS",
        )
    elif blocked:
        add(
            "residency_mode",
            "数据不出域模式",
            "数据合规",
            "fail",
            "strict 模式下仍有出站目标不在白名单："
            + "、".join(f"{t['purpose']}({t['host']})" for t in blocked),
            "把这些域名加入 OUTBOUND_ALLOWED_HOSTS，或改为内网/本地模型地址",
        )
    else:
        add(
            "residency_mode",
            "数据不出域模式",
            "数据合规",
            "pass",
            "strict 模式已开启，全部出站目标均在白名单或内网",
        )

    # ---------- 错误上报 PII ----------
    if settings.is_sentry_enabled and settings.SENTRY_SEND_DEFAULT_PII:
        add(
            "sentry_pii",
            "错误上报个人信息",
            "数据合规",
            "fail",
            "已启用 Sentry 且 SENTRY_SEND_DEFAULT_PII=true：IP/Cookie 等会离开本域",
            "合规环境请设 SENTRY_SEND_DEFAULT_PII=false，或使用自建 Sentry",
        )
    else:
        add(
            "sentry_pii",
            "错误上报个人信息",
            "数据合规",
            "pass",
            "未启用 Sentry，或已关闭 PII 上报",
        )

    # ---------- 审计 ----------
    if not settings.AUDIT_ENABLED:
        add(
            "audit_enabled",
            "审计日志",
            "审计",
            "fail",
            "AUDIT_ENABLED=false：敏感动作不会留痕",
            "生产环境请开启审计（AUDIT_ENABLED=true）",
        )
    elif settings.AUDIT_RETENTION_DAYS < 180:
        add(
            "audit_enabled",
            "审计日志",
            "审计",
            "warn",
            f"审计已开启，但保留期仅 {settings.AUDIT_RETENTION_DAYS} 天（等保常见要求 ≥180 天）",
            "调大 AUDIT_RETENTION_DAYS，并确认备份也覆盖 audit_logs 表",
        )
    else:
        add(
            "audit_enabled",
            "审计日志",
            "审计",
            "pass",
            f"审计已开启，保留 {settings.AUDIT_RETENTION_DAYS} 天",
        )

    # ---------- 鉴权与密钥 ----------
    add(
        "auth_required",
        "接口鉴权",
        "访问控制",
        "pass" if settings.AUTH_REQUIRED else "fail",
        "所有业务接口要求 JWT 鉴权" if settings.AUTH_REQUIRED else "AUTH_REQUIRED=false：所有请求回退 Demo 用户",
        "生产环境必须设 AUTH_REQUIRED=true",
    )
    add(
        "jwt_secret",
        "JWT 签名密钥",
        "访问控制",
        "pass" if settings.is_jwt_configured else "fail",
        "已使用 ≥32 字节的随机密钥" if settings.is_jwt_configured else "仍为占位密钥或长度不足 32 字节，令牌可被伪造",
        "用 python -c \"import secrets;print(secrets.token_urlsafe(48))\" 生成并注入环境变量",
    )

    # ---------- 暴露面 ----------
    add(
        "docs_exposed",
        "接口文档暴露",
        "暴露面",
        "pass" if settings.APP_ENV != "development" else "warn",
        "生产环境已关闭 /docs" if settings.APP_ENV != "development" else "当前 APP_ENV=development，/docs 对外可访问",
        "生产部署设 APP_ENV=production",
    )
    add(
        "metrics_token",
        "监控指标保护",
        "暴露面",
        "pass" if (not settings.METRICS_ENABLED or settings.METRICS_TOKEN) else "warn",
        "未启用指标" if not settings.METRICS_ENABLED else (
            "已设置抓取令牌" if settings.METRICS_TOKEN else "/metrics 未设置令牌，暴露接口拓扑与流量规模"
        ),
        "生产环境建议设置 METRICS_TOKEN，并在 nginx 侧限制来源网段",
    )
    add(
        "https",
        "传输加密（HTTPS）",
        "暴露面",
        "warn",
        "应用本身不感知 TLS，需在 nginx/网关终止 HTTPS",
        "按 docker-compose.https.yml 配置证书；内网私有化也应使用内部 CA",
    )

    # ---------- 备份与持久化 ----------
    status = backup_service.backup_status()
    if not status["enabled"]:
        add(
            "backup_enabled",
            "数据库备份",
            "业务连续性",
            "warn",
            "备份功能未启用",
            "生产环境请启用 BACKUP_ENABLED 并部署定时备份容器/CronJob",
        )
    elif status["last"] is None:
        add(
            "backup_enabled",
            "数据库备份",
            "业务连续性",
            "warn",
            "备份已启用，但尚无任何备份文件",
            "在状态页点「立即备份」，或等待定时任务首次执行",
        )
    elif not status["last"].get("verified"):
        add(
            "backup_enabled",
            "数据库备份",
            "业务连续性",
            "warn",
            "最近一次备份未通过完整性校验",
            "检查磁盘空间与 pg_dump 版本是否与服务端一致",
        )
    else:
        add(
            "backup_enabled",
            "数据库备份",
            "业务连续性",
            "pass",
            f"最近备份 {status['last']['created_at']}，共 {status['count']} 份",
        )

    redis_status = await backup_service.redis_persistence_status()
    if not redis_status["reachable"]:
        add(
            "redis_persistence",
            "Redis 持久化",
            "业务连续性",
            "warn",
            f"Redis 不可达（{redis_status['detail']}），无法确认持久化配置",
            "确认 Redis 已启动；生产启用 --appendonly yes",
        )
    elif redis_status["persistence_ok"]:
        add(
            "redis_persistence",
            "Redis 持久化",
            "业务连续性",
            "pass",
            "AOF 已开启" if redis_status["aof_enabled"] else "RDB 最近一次落盘成功",
        )
    else:
        add(
            "redis_persistence",
            "Redis 持久化",
            "业务连续性",
            "fail",
            "既未开启 AOF，RDB 最近一次也未成功：容器重启会丢失排队中的任务",
            "Redis 启动参数加 --appendonly yes（见 docker-compose.yml）",
        )

    # ---------- 模型可用性（影响检索质量而非合规，归入「能力」类） ----------
    add(
        "model_keys",
        "模型 API Key",
        "能力可用性",
        "pass" if (settings.is_deepseek_configured and settings.is_qwen_configured) else "warn",
        "对话与向量模型均已配置"
        if (settings.is_deepseek_configured and settings.is_qwen_configured)
        else "存在未配置的模型 Key（对话或向量能力会降级）",
        "在 .env 中填写 DEEPSEEK_API_KEY 与 QWEN_API_KEY",
    )

    summary = {"pass": 0, "warn": 0, "fail": 0}
    for item in items:
        summary[item["status"]] = summary.get(item["status"], 0) + 1

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": residency["mode"],
        "region": residency["region"],
        "summary": summary,
        "items": items,
    }
