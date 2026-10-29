# 运维接口（P7-B1 / P7-B2 / P7-Dep1）
# ---------------------------------------------------------------------
# 职责：给管理端「系统状态」页提供数据，并暴露 Prometheus 抓取端点。
#
# 接口清单：
#   GET  /ops/status                 系统状态总览（依赖健康 / LLM 配额 / 备份 / 审计 / 合规拓扑）
#   GET  /ops/audit-logs             审计日志分页查询（按动作/结果/用户/时间/关键字）
#   GET  /ops/audit-logs/actions     审计动作字典（前端筛选下拉）
#   GET  /ops/backups                备份文件列表
#   POST /ops/backups                立即执行一次备份（管理员手动触发）
#   POST /ops/backups/prune          按保留策略清理过期备份
#   GET  /ops/compliance             合规自检报告
#   POST /ops/quotas/{user_id}/reset 重置某用户的限流计数（应急解封）
#   GET  /metrics                    Prometheus 指标（可选令牌保护，不要求管理员）
#
# 权限设计：
#   - /ops/* 全部挂在路由级 require_admin（RBAC）：状态、备份、审计都属于运维面；
#   - /metrics 不挂 admin：Prometheus 用固定 token 抓取，没有用户身份；
#     若配置了 METRICS_TOKEN 则校验令牌，否则允许匿名（内网部署常见）。
#
# 审计设计：
#   查审计日志本身也是敏感动作（能看出谁在调查谁），因此 /ops/audit-logs
#   同样写一条 ops.audit.query 审计；备份与配额重置同理。

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..core import metrics as metrics_module
from ..core import rate_limit
from ..core.errors import BackupError
from ..core.logging import get_logger
from ..core.security import require_admin
from ..db.engine import get_db
from ..db.models import User
from ..schemas import (
    AuditActionOption,
    AuditLogListResponse,
    AuditLogOut,
    BackupItem,
    BackupStatus,
    ComplianceReport,
    OpsStatusResponse,
)
from ..services import audit_service, backup_service, ops_service

logger = get_logger(__name__)

# 管理端运维路由：路由级依赖保证「仅管理员」，单个端点无需重复声明
router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(require_admin)])

# 指标路由：单独一个 router，不挂 admin（由 token 或内网策略保护）
metrics_router = APIRouter(tags=["ops"])


@router.get(
    "/status",
    response_model=OpsStatusResponse,
    summary="系统状态总览（管理员）",
    description=(
        "聚合依赖健康、LLM 限流配额、备份状态、审计概览与数据不出域拓扑。"
        "各项探测均带 2 秒超时，单个依赖故障不会拖垮整页。"
    ),
)
async def get_status(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> OpsStatusResponse:
    """返回系统状态总览。

    说明：quota（配额用量）展示的是**当前登录管理员自己**的用量——
    配额是按用户计的，展示「别人的」既不准确也没有意义；
    要看全局用量应去 Grafana（knowagent_llm_tokens_total）。
    """
    payload = await ops_service.collect_status(db, user_id=admin.id)
    return OpsStatusResponse(**payload)


@router.get(
    "/audit-logs",
    response_model=AuditLogListResponse,
    summary="审计日志查询（管理员）",
    description="支持按动作、结果、操作者、资源与时间范围筛选，按时间倒序分页。",
)
async def list_audit_logs(
    request: Request,
    action: Optional[str] = Query(default=None, description="动作代码精确匹配"),
    result: Optional[str] = Query(default=None, pattern="^(success|failure)$", description="结果"),
    user_id: Optional[str] = Query(default=None, description="操作者用户 ID"),
    resource_type: Optional[str] = Query(default=None, description="资源类型"),
    resource_id: Optional[str] = Query(default=None, description="资源标识"),
    keyword: Optional[str] = Query(default=None, max_length=128, description="关键字模糊搜索"),
    start: Optional[datetime] = Query(default=None, description="起始时间（ISO 8601）"),
    end: Optional[datetime] = Query(default=None, description="结束时间（ISO 8601）"),
    limit: int = Query(default=50, ge=1, le=200, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AuditLogListResponse:
    """分页查询审计日志，并把「查询行为」本身记入审计。

    Raises:
        HTTPException 400: user_id 不是合法 UUID（避免把非法值传到 SQL 层）。
    """
    settings = get_settings()

    # 提前校验 UUID 格式：非法值会让 SQL 报错并返回 500，这里转成明确的 400
    if user_id:
        try:
            UUID(user_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="user_id 必须是合法 UUID"
            ) from exc

    rows, total = await audit_service.list_audit_logs(
        db,
        action=action,
        result=result,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        keyword=keyword,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )

    # 敏感读操作留痕：只在开启「读审计」或明确查询审计时记录。
    # 这里刻意**不受 AUDIT_LOG_READS 控制**——查审计日志属于高敏感动作，必须留痕。
    await audit_service.record(
        "ops.audit.query",
        user=admin,
        resource_type="audit",
        detail={
            "filters": {
                "action": action,
                "result": result,
                "keyword": keyword,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
            },
            "returned": len(rows),
            "total": total,
            "client": request.client.host if request.client else None,
            "audit_enabled": settings.AUDIT_ENABLED,
        },
    )

    return AuditLogListResponse(
        items=[AuditLogOut(**audit_service.to_payload(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/audit-logs/actions",
    response_model=List[AuditActionOption],
    summary="审计动作字典（管理员）",
    description="返回全部可审计动作，供前端筛选下拉使用（前后端共用一份字典）。",
)
async def list_audit_actions() -> List[AuditActionOption]:
    """返回动作字典（静态数据，无需查库）。"""
    return [AuditActionOption(**item) for item in audit_service.list_actions()]


@router.get(
    "/backups",
    summary="备份文件列表（管理员）",
    description="列出备份目录下的备份文件（按时间倒序），含完整性校验结果与恢复命令提示。",
)
async def list_backups(
    limit: int = Query(default=50, ge=1, le=200, description="最多返回条数"),
) -> dict:
    """返回备份列表与总览。

    返回结构：{"status": BackupStatus, "items": [BackupItem...]}
    之所以把总览和列表放在一起：前端一个卡片同时要展示「策略 + 最近备份 + 列表」，
    分成两个接口会让状态页多发一次请求。
    """
    status_payload = backup_service.backup_status()
    items = backup_service.list_backups(limit=limit)
    return {
        "status": BackupStatus(**status_payload),
        "items": [BackupItem(**item) for item in items],
    }


@router.post(
    "/backups",
    response_model=BackupItem,
    summary="立即执行一次备份（管理员）",
    description=(
        "同步执行 pg_dump + gzip，返回备份文件信息。大库可能耗时数十秒到数分钟，"
        "请在前端给出「执行中」提示；日常应依赖定时任务而非手工点击。"
    ),
)
async def trigger_backup(
    admin: User = Depends(require_admin),
) -> BackupItem:
    """手动触发一次数据库备份。

    Raises:
        HTTPException 500: pg_dump 不可用 / 执行失败 / 校验不通过（detail 为可读原因）。
    """
    try:
        item = await backup_service.run_backup(trigger="manual", operator=admin.username)
    except BackupError as exc:
        # 失败也要审计：备份失败是需要追责/排查的运维事件
        await audit_service.record(
            "ops.backup.trigger",
            result="failure",
            user=admin,
            resource_type="backup",
            detail={"reason": exc.message},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.message
        ) from exc

    await audit_service.record(
        "ops.backup.trigger",
        user=admin,
        resource_type="backup",
        resource_id=item["name"],
        detail={"size_bytes": item["size_bytes"], "trigger": "manual"},
    )
    return BackupItem(**item)


@router.post(
    "/backups/prune",
    summary="清理过期备份（管理员）",
    description="按「保留天数 + 保留个数」双约束删除过期备份，返回删除数量。",
)
async def prune_backups(
    admin: User = Depends(require_admin),
) -> dict:
    """按保留策略清理备份文件。"""
    removed = backup_service.prune_backups()
    await audit_service.record(
        "ops.backup.prune",
        user=admin,
        resource_type="backup",
        detail={"removed": removed},
    )
    return {"removed": removed, "status": BackupStatus(**backup_service.backup_status())}


@router.get(
    "/compliance",
    response_model=ComplianceReport,
    summary="合规自检报告（管理员）",
    description=(
        "逐项检查数据不出域、审计留存、鉴权与密钥、暴露面、备份与 Redis 持久化，"
        "输出 pass/warn/fail 与修复建议。私有化交付验收时可直接截图留档。"
    ),
)
async def get_compliance_report() -> ComplianceReport:
    """生成合规自检报告（只读，不产生审计——巡检本身不应刷屏审计表）。"""
    payload = await ops_service.build_compliance_report()
    return ComplianceReport(**payload)


@router.post(
    "/quotas/{user_id}/reset",
    summary="重置用户配额（管理员）",
    description="清空某用户的分钟/日次数与 token 计数，用于误封或客户支持的应急解封。",
)
async def reset_user_quota(
    user_id: str,
    admin: User = Depends(require_admin),
) -> dict:
    """重置指定用户的限流计数。

    Raises:
        HTTPException 400: user_id 非法。
    """
    try:
        UUID(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="user_id 必须是合法 UUID"
        ) from exc

    await rate_limit.reset_quota(user_id)
    await audit_service.record(
        "ops.quota.reset",
        user=admin,
        resource_type="user",
        resource_id=user_id,
    )
    # 回传重置后的状态，前端可直接刷新展示
    quota = await rate_limit.get_quota_status(user_id)
    return {"user_id": user_id, "quota": quota.__dict__}


@metrics_router.get(
    "/metrics",
    summary="Prometheus 指标",
    description=(
        "输出本服务的 Prometheus 指标文本。若配置了 METRICS_TOKEN，"
        "需携带 Authorization: Bearer <token> 或 ?token=。"
    ),
    include_in_schema=False,
)
async def prometheus_metrics(
    request: Request,
    token: Optional[str] = Query(default=None, description="抓取令牌（未配置时忽略）"),
) -> Response:
    """输出指标文本。

    鉴权策略：
        - METRICS_TOKEN 为空：直接放行（适合内网/仅 nginx 内网可达的部署）；
        - 非空：校验请求头或 query 上的令牌，不匹配返回 404（而不是 401）——
          对扫描器「路径不存在」比「需要认证」更少暴露信息。
    """
    settings = get_settings()
    if not settings.METRICS_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="指标未启用")

    expected = (settings.METRICS_TOKEN or "").strip()
    if expected:
        header = request.headers.get("authorization", "")
        provided = token or (header[7:] if header.lower().startswith("bearer ") else "")
        if provided != expected:
            # 用 404 隐藏端点存在性（与 nginx 的 deny 行为保持一致）
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    payload, content_type = metrics_module.render()
    return Response(content=payload, media_type=content_type)
