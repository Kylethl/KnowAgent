# /notifications 站内信接口（P6-B1）
# ---------------------------------------------------------------------
# 职责：为前端顶栏铃铛（P6-F1）提供「通知列表 / 未读数 / 标记已读」能力。
#
# 接口契约（与前端 src/services/notificationApi.ts、src/types.ts 严格对齐）：
#   GET  /notifications                  通知列表（可分页、可只看未读）
#   GET  /notifications/unread-count     未读数（轻量轮询）
#   POST /notifications/{id}/read        单条标记已读
#   POST /notifications/read-all         全部标记已读
#
# 鉴权：全部要求登录（get_current_user_id）。
#       通知是「发给某个人」的私信，只按令牌解析出的 user_id 查询，
#       客户端传任何身份参数都无效——这是通知类接口最基本的安全底线。
#
# 为什么单独有 unread-count 接口：
#   前端每 30 秒轮询一次角标。若每次都拉完整列表，正文（可能含长驳回理由）
#   会被反复传输，数据库也要扫整行。只回一个数字最省，且能配合部分索引。

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import NotificationNotFound
from ..core.logging import get_logger
from ..core.security import get_current_user_id
from ..db.engine import get_db
from ..schemas import (
    NotificationListResponse,
    NotificationOut,
    NotificationReadAllResponse,
    NotificationReadResponse,
    NotificationUnreadResponse,
)
from ..services import notification_service as svc

logger = get_logger(__name__)

# 路由集合；main.py 中以 /notifications 前缀统一注册
router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get(
    "",
    response_model=NotificationListResponse,
    summary="通知列表（站内信）",
    description=(
        "返回当前登录用户的通知，按时间倒序。"
        "unread_only=true 时只返回未读通知；unread_count 始终为全部未读数（不受筛选影响）。"
    ),
)
async def list_notifications(
    unread_only: bool = Query(default=False, description="是否只看未读"),
    limit: Optional[int] = Query(default=None, ge=1, le=50, description="每页条数（1~50）"),
    offset: Optional[int] = Query(default=None, ge=0, description="偏移量"),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    """查询当前用户的站内信。

    说明：未读数单独统计（见 notification_service.list_notifications），
    因此前端筛「只看未读」时角标仍显示真实未读总量，不会自相矛盾。
    """
    items, total, unread = await svc.list_notifications(
        db, user_id=user_id, unread_only=unread_only, limit=limit, offset=offset
    )
    real_limit, real_offset = svc.normalize_pagination(limit, offset)

    return NotificationListResponse(
        items=[NotificationOut(**svc.to_payload(n)) for n in items],
        total=total,
        unread_count=unread,
        limit=real_limit,
        offset=real_offset,
    )


@router.get(
    "/unread-count",
    response_model=NotificationUnreadResponse,
    summary="未读通知数（轮询用）",
    description="只返回未读条数，供前端定时刷新顶栏角标，避免重复拉取通知正文。",
)
async def get_unread_count(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> NotificationUnreadResponse:
    """返回当前用户的未读通知数。"""
    unread = await svc.count_unread(db, user_id)
    return NotificationUnreadResponse(unread_count=unread)


@router.post(
    "/{notification_id}/read",
    response_model=NotificationReadResponse,
    summary="标记单条通知已读",
    description="把指定通知标记为已读（幂等）。响应同时回传最新未读数，前端无需再请求一次。",
)
async def mark_notification_read(
    notification_id: str,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> NotificationReadResponse:
    """标记单条通知已读。

    Raises:
        HTTPException 404: 通知不存在、已被清理，或不属于当前用户。
    """
    try:
        notification = await svc.mark_read(db, user_id, notification_id)
    except NotificationNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc

    unread = await svc.count_unread(db, user_id)
    return NotificationReadResponse(
        notification=NotificationOut(**svc.to_payload(notification)),
        unread_count=unread,
    )


@router.post(
    "/read-all",
    response_model=NotificationReadAllResponse,
    summary="全部标记已读",
    description="把当前用户的全部未读通知一次性标记为已读，返回实际更新条数。",
)
async def mark_all_notifications_read(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> NotificationReadAllResponse:
    """一键清空未读角标。

    说明：即使没有未读也返回 200（updated=0），
    前端「全部已读」按钮无需区分「有没有可读的」，避免多余的分支判断。
    """
    updated = await svc.mark_all_read(db, user_id)
    logger.info("用户 %s 一键已读通知：%d 条", user_id, updated)
    return NotificationReadAllResponse(updated=updated, unread_count=0)
