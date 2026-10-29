# 审计日志服务（P7-B2：合规）
# ---------------------------------------------------------------------
# 职责：把「敏感动作」写成一条结构化审计记录（audit_logs 表 + 一条日志），
#       并提供管理端查询、汇总、过期清理能力。
#
# 与普通日志的分工（这个边界必须清楚，否则两边都会写歪）：
#   - core/logging：面向排障，记录每个请求的路径/状态/耗时，量大、留存短；
#   - 本模块：面向合规举证，只记「谁对什么做了什么」，结构稳定、留存长（默认 180 天）。
#
# 记录哪些动作（见 ACTIONS）：
#   auth.*      登录成功/失败、注册            —— 账号安全审计的基线
#   document.*  上传、审核、重入库、下线        —— 知识库内容变更的追溯链
#   ops.*       手动备份、审计查询、配额重置     —— 运维动作同样要留痕
#   对话（llm.chat）默认不审计：量大且价值低，由 AUDIT_LOG_READS 显式开启。
#
# 三条硬约定：
#   1. **绝不写敏感值**：detail 里的 password / token / api_key / secret 等键
#      会被 _redact 递归抹成 "***"；请求体从不进入审计。
#   2. **写审计绝不阻断业务**：数据库异常、字段超长等都只记 warning，
#      因为「审计写失败」不应该让用户上传不了文档（但会在日志里明确告警）。
#   3. **身份用快照**：username/role 在写入时刻固化，用户改名/提权不改写历史。

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..core import metrics, request_context
from ..core.logging import get_logger
from ..db.engine import SessionLocal
from ..db.models import AuditLog, User

logger = get_logger(__name__)

# ---------------------------------------------------------------------
# 动作字典：集中登记，前端筛选下拉与后端校验共用同一份来源
# ---------------------------------------------------------------------
# 说明：value 是写库的 action 代码（稳定标识，不可随意改），label 给管理端展示。
ACTIONS: Dict[str, str] = {
    # ---- 认证 ----
    "auth.register": "用户注册",
    "auth.login": "登录成功",
    "auth.login_failed": "登录失败",
    # ---- 文档 ----
    "document.upload": "上传文档",
    "document.review": "审核文档",
    "document.reindex": "重新入库",
    "document.unpublish": "下线文档",
    # ---- 运维 ----
    "ops.backup.trigger": "手动备份",
    "ops.backup.prune": "清理过期备份",
    "ops.audit.query": "查询审计日志",
    "ops.quota.reset": "重置用户配额",
    # ---- 对话（默认不记，AUDIT_LOG_READS=true 时开启）----
    "llm.chat": "发起对话",
}

# 需要脱敏的字段名关键字（小写匹配）：命中即整值替换为 "***"
_SENSITIVE_KEYWORDS = (
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
)


def _redact(value: Any, depth: int = 0) -> Any:
    """递归脱敏 detail 中的敏感字段。

    为什么需要它：
        调用方可能图省事把整个请求体塞进 detail，而请求体里可能有密码；
        在写入侧统一切断这条路，比要求每个调用点「记得别传密码」可靠得多。

    Args:
        value: 任意 JSON 可序列化值。
        depth: 递归深度（防御性限制，避免异常嵌套导致栈溢出）。

    Returns:
        脱敏后的值（dict/list 递归处理，敏感键的值替换为 "***"）。
    """
    if depth > 6:
        # 超过 6 层的结构在审计里没有价值，直接截断为字符串
        return str(value)[:200]
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if any(word in key_str.lower() for word in _SENSITIVE_KEYWORDS):
                cleaned[key_str] = "***"
            else:
                cleaned[key_str] = _redact(item, depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value[:50]]  # 列表最多保留 50 项
    if isinstance(value, (str, int, float, bool)) or value is None:
        # 字符串截断：detail 是 JSONB，超长文本既无用又拖慢查询
        return value[:500] if isinstance(value, str) else value
    # 其余类型（UUID / datetime / ORM 对象等）统一转字符串，保证可入库
    return str(value)[:200]


def _resolve_actor(
    user: Optional[User],
    user_id: Optional[Any],
    username: Optional[str],
    role: Optional[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """解析操作者身份，优先用传入对象，其次用上下文，最后留空。

    Args:
        user: User ORM 对象（最完整的身份来源）。
        user_id: 显式用户 ID（无 User 对象时使用）。
        username: 显式用户名。
        role: 显式角色。

    Returns:
        (user_id 字符串或 None, username, role)
    """
    # 1) 有 User 对象：直接取三个字段
    if user is not None:
        return str(user.id), user.username, user.role
    # 2) 无对象：用显式参数补齐；缺失的字段回退到请求上下文（鉴权依赖写入的）
    resolved_id = str(user_id) if user_id else (request_context.get_user_id() or None)
    resolved_name = username or (request_context.get_username() or None)
    return resolved_id, resolved_name, role


async def record(
    action: str,
    *,
    result: str = "success",
    user: Optional[User] = None,
    user_id: Optional[Any] = None,
    username: Optional[str] = None,
    role: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[Any] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """写入一条审计记录（best-effort，绝不抛出异常）。

    调用时机：动作**成功或明确失败之后**。失败的动作用 result="failure" 记录，
    这是安全审计里最有价值的部分（能看出谁在反复尝试越权）。

    Args:
        action: 动作代码，建议取自 ACTIONS 的键。
        result: success / failure。
        user: 操作者 ORM 对象（推荐传入，身份最完整）。
        user_id: 显式用户 ID（无对象时使用）。
        username: 显式用户名。
        role: 显式角色（anonymous 用于未登录尝试）。
        resource_type: 资源类型（document / message / backup / ...）。
        resource_id: 资源标识（UUID 或文件名等）。
        detail: 动作细节（会自动脱敏）。
    """
    settings = get_settings()

    # 统一收敛为成功/失败两种取值，避免脏值写库触发 CHECK 约束
    normalized_result = "failure" if str(result).lower() == "failure" else "success"
    actor_id, actor_name, actor_role = _resolve_actor(user, user_id, username, role)
    context = request_context.snapshot()

    # 无论是否落库，都先打一条结构化日志：
    # 这样即使 audit_logs 表不可用，动作痕迹仍在日志里（双通道留痕）。
    logger.info(
        "审计事件：action=%s result=%s actor=%s resource=%s",
        action,
        normalized_result,
        actor_name or actor_id or "anonymous",
        f"{resource_type}:{resource_id}" if resource_type else "-",
        extra={
            "audit_action": action,
            "audit_result": normalized_result,
            "resource_type": resource_type or "",
            "resource_id": str(resource_id) if resource_id else "",
        },
    )

    # 指标：按动作计数（只看趋势，不含身份，避免基数爆炸）
    metrics.inc_audit(action)

    if not settings.AUDIT_ENABLED:
        return

    row = AuditLog(
        # 空字符串要转 None，否则外键会因 "" 不是合法 UUID 而报错
        user_id=UUID(actor_id) if actor_id else None,
        # 各字段按列宽截断，避免「用户名校验 64 字符」这类问题把审计写失败
        username=actor_name[:64] if actor_name else None,
        role=actor_role[:16] if actor_role else None,
        action=action[:64],
        resource_type=resource_type[:32] if resource_type else None,
        resource_id=str(resource_id)[:128] if resource_id else None,
        result=normalized_result,
        ip=context["ip"] or None,
        user_agent=context["user_agent"][:512] if context["user_agent"] else None,
        request_id=context["request_id"] or None,
        detail=_redact(detail) if detail else None,
    )

    try:
        # 独立会话：审计不能随业务事务一起回滚——
        # 「审核失败被回滚」恰恰是需要留痕的场景。
        async with SessionLocal() as session:
            session.add(row)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 审计失败不得影响业务
        logger.warning("审计日志写入失败（已忽略，业务不受影响）：action=%s，原因=%s", action, exc)


async def record_failure(action: str, reason: str, **kwargs: Any) -> None:
    """写入一条「失败」审计（如登录失败、越权尝试）。

    Args:
        action: 动作代码。
        reason: 失败原因（会写入 detail.reason，注意不要放敏感信息）。
        **kwargs: 透传给 record() 的其它字段。
    """
    detail = dict(kwargs.pop("detail", None) or {})
    detail["reason"] = reason
    await record(action, result="failure", detail=detail, **kwargs)


def to_payload(row: AuditLog) -> Dict[str, Any]:
    """把 ORM 行转换为对外结构（供 API / 前端直接使用）。

    Args:
        row: AuditLog 行。

    Returns:
        与前端 AuditLogItem 字段一致的字典（时间统一 ISO 8601）。
    """
    return {
        "id": str(row.id),
        "user_id": str(row.user_id) if row.user_id else None,
        "username": row.username,
        "role": row.role,
        "action": row.action,
        # 动作中文名取自字典；未登记的动作原样返回，避免展示空白
        "action_label": ACTIONS.get(row.action, row.action),
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "result": row.result,
        "ip": row.ip,
        "request_id": row.request_id,
        "detail": row.detail or None,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


async def list_audit_logs(
    db: AsyncSession,
    *,
    action: Optional[str] = None,
    result: Optional[str] = None,
    user_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    keyword: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[AuditLog], int]:
    """分页查询审计日志（管理端使用）。

    Args:
        db: 数据库会话。
        action: 动作代码精确匹配。
        result: success / failure。
        user_id: 操作者 ID。
        resource_type: 资源类型。
        resource_id: 资源标识。
        keyword: 关键字（模糊匹配 username / action / resource_id）。
        start: 起始时间（含）。
        end: 结束时间（含）。
        limit: 每页条数（调用方已限制范围）。
        offset: 偏移量。

    Returns:
        (当前页行列表, 满足条件的总数)。
    """
    # 基础查询 + 逐个可选条件拼装，避免把 None 传成字符串条件
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if result:
        conditions.append(AuditLog.result == result)
    if user_id:
        conditions.append(AuditLog.user_id == UUID(user_id))
    if resource_type:
        conditions.append(AuditLog.resource_type == resource_type)
    if resource_id:
        conditions.append(AuditLog.resource_id == str(resource_id))
    if start:
        conditions.append(AuditLog.created_at >= start)
    if end:
        conditions.append(AuditLog.created_at <= end)
    if keyword:
        # 关键字三字段 OR：覆盖「搜用户 / 搜动作 / 搜资源」
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                AuditLog.username.ilike(pattern),
                AuditLog.action.ilike(pattern),
                AuditLog.resource_id.ilike(pattern),
            )
        )

    base = select(AuditLog)
    count_query = select(func.count()).select_from(AuditLog)
    for condition in conditions:
        base = base.where(condition)
        count_query = count_query.where(condition)

    total = int((await db.execute(count_query)).scalar() or 0)
    rows = (
        (
            await db.execute(
                base.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def build_summary(db: AsyncSession, days: int = 7) -> Dict[str, Any]:
    """统计审计概览（状态页卡片用）。

    Args:
        db: 数据库会话。
        days: 统计窗口（天），默认近 7 天。

    Returns:
        形如 {total, today, failures, last_event_at, top_actions: [{action,label,count}]}。
    """
    since = datetime.now().astimezone() - timedelta(days=days)
    today_start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

    total = int(
        (await db.execute(select(func.count()).select_from(AuditLog))).scalar() or 0
    )
    today = int(
        (
            await db.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.created_at >= today_start)
            )
        ).scalar()
        or 0
    )
    failures = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.result == "failure", AuditLog.created_at >= since)
            )
        ).scalar()
        or 0
    )
    last_event_at = (
        await db.execute(select(func.max(AuditLog.created_at)))
    ).scalar()

    # 动作分布：近 N 天各动作条数，取前 8 个（前端做小条形图）
    rows = (
        await db.execute(
            select(AuditLog.action, func.count().label("count"))
            .where(AuditLog.created_at >= since)
            .group_by(AuditLog.action)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()

    return {
        "total": total,
        "today": today,
        "failures": failures,
        "last_event_at": last_event_at.isoformat() if last_event_at else None,
        "window_days": days,
        "top_actions": [
            {
                "action": row[0],
                "label": ACTIONS.get(row[0], row[0]),
                "count": int(row[1]),
            }
            for row in rows
        ],
    }


async def prune(db: AsyncSession, retention_days: int) -> int:
    """清理超过保留期的审计记录。

    说明：保留期是合规参数（等保通常要求 ≥180 天），因此**不做默认删除**——
    只有当 retention_days > 0 时才执行，配置为 0 表示永久保留。

    Args:
        db: 数据库会话。
        retention_days: 保留天数（≤0 表示不清理）。

    Returns:
        实际删除的条数。
    """
    if retention_days <= 0:
        return 0
    cutoff = datetime.now().astimezone() - timedelta(days=retention_days)
    result = await db.execute(AuditLog.__table__.delete().where(AuditLog.created_at < cutoff))
    await db.commit()
    deleted = int(result.rowcount or 0)
    if deleted:
        logger.info("审计日志清理完成：删除 %d 条（保留 %d 天）", deleted, retention_days)
    return deleted


def list_actions() -> List[Dict[str, str]]:
    """返回全部可审计动作（前端筛选下拉的数据源）。

    Returns:
        [{"value": "auth.login", "label": "登录成功"}, ...]
    """
    return [{"value": key, "label": label} for key, label in ACTIONS.items()]


def redact_preview(detail: Optional[dict]) -> Optional[dict]:
    """对外暴露的脱敏工具（导出/展示前再兜一层）。"""
    return _redact(detail) if detail else None
