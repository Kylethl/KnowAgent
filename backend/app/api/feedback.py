# /messages/{id}/feedback 答案反馈接口（P6-B2）
# ---------------------------------------------------------------------
# 职责：让用户对助手的某条回答点赞/点踩，并让管理员看到反馈汇总。
#
# 接口契约（与前端 src/services/feedbackApi.ts、src/types.ts 严格对齐）：
#   POST   /messages/{message_id}/feedback     提交 / 修改反馈
#   GET    /messages/{message_id}/feedback     查询当前用户的反馈（回显按钮状态）
#   DELETE /messages/{message_id}/feedback     取消反馈（点错了可撤销）
#   GET    /admin/feedback/summary             质量评估概览（仅管理员）
#
# 鉴权与隔离：
#   - 全部要求登录；评价人一律取自 JWT，不接受客户端传入 user_id；
#   - 归属校验在服务层完成（消息所属会话必须属于当前用户），
#     因此 A 无法给 B 的回答打分，也无法用本接口探测他人会话是否存在（统一 404）。
#
# 关于「评价对象」：只允许 role='assistant' 的消息，见 feedback_service。

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import FeedbackNotAllowed, MessageNotFound
from ..core.logging import get_logger
from ..core.security import get_current_user_id, require_admin
from ..db.engine import get_db
from ..db.models import User
from ..schemas import (
    FeedbackOut,
    FeedbackRequest,
    FeedbackStateResponse,
    FeedbackSummaryResponse,
)
from ..services import feedback_service as svc

logger = get_logger(__name__)

# 用户侧路由：/messages/...（不需要前缀，路径本身就是完整语义）
router = APIRouter(tags=["feedback"])

# 管理员侧路由：单独一个 router 的原因——
#   feedback 汇总属于「答案质量评估」，与 admin.py 的文档审核不是同一件事；
#   但同样要求管理员身份，故直接挂 require_admin 依赖，与 /admin/* 保持一致。
admin_router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.post(
    "/messages/{message_id}/feedback",
    response_model=FeedbackOut,
    summary="提交 / 修改答案反馈",
    description=(
        "对一条助手回答点赞（up）或点踩（down）。"
        "同一用户对同一条回答只有一条反馈，重复提交视为修改；"
        "点踩可附带归因（off_topic / wrong / unsupported / other）与补充说明。"
    ),
)
async def submit_feedback(
    message_id: str,
    payload: FeedbackRequest,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> FeedbackOut:
    """提交或修改反馈。

    Raises:
        HTTPException 404: 消息不存在 / 不属于当前用户 / 会话已删除。
        HTTPException 409: 该消息不是助手回答，或入参不被接受（归因非法 / 备注超长）。
    """
    try:
        feedback = await svc.submit_feedback(
            db=db,
            user_id=user_id,
            message_id=message_id,
            rating=payload.rating,
            reason=payload.reason,
            comment=payload.comment,
        )
    except MessageNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except FeedbackNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc

    return FeedbackOut(**svc.to_payload(feedback))


@router.get(
    "/messages/{message_id}/feedback",
    response_model=FeedbackStateResponse,
    summary="查询答案反馈（当前用户）",
    description=(
        "返回当前用户对指定回答的反馈；未评价时 feedback 为 null。"
        "用于刷新页面后恢复点赞/点踩按钮的选中状态。"
    ),
)
async def get_feedback(
    message_id: str,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> FeedbackStateResponse:
    """查询当前用户对某条回答的反馈。

    说明：消息已不存在时不报 404，而是返回 feedback=null——
    历史页面回显不应因为某条旧消息被清理而整体失败。
    """
    feedback = await svc.get_feedback(db, user_id, message_id)
    return FeedbackStateResponse(
        message_id=message_id,
        feedback=FeedbackOut(**svc.to_payload(feedback)) if feedback else None,
    )


@router.delete(
    "/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="取消答案反馈",
    description="删除当前用户对该回答的反馈（用于「点错了想撤销」）。幂等：没有反馈也返回 204。",
)
async def delete_feedback(
    message_id: str,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    """取消反馈：物理删除当前用户的评价记录。

    说明：幂等设计——本来就没评价过也返回 204，
    前端「再次点击同一按钮即取消」的交互无需先查询状态。
    """
    deleted = await svc.delete_feedback(db, user_id, message_id)
    if not deleted:
        logger.debug("取消反馈：无记录可删（message=%s，user=%s）", message_id, user_id)
    return None


@admin_router.get(
    "/feedback/summary",
    response_model=FeedbackSummaryResponse,
    summary="答案质量评估概览（管理员）",
    description=(
        "汇总全部用户反馈：总数 / 好评率 / 点踩归因分布 / 最近差评明细（含提问与回答摘要）。"
        "《需求清单》P6 功能2 要求反馈「用于后续 prompt/检索离线优化」，本接口即该优化的数据入口。"
    ),
)
async def feedback_summary(
    recent_limit: Optional[int] = Query(
        default=10, ge=1, le=50, description="最近差评返回条数（1~50）"
    ),
    db: AsyncSession = Depends(get_db),
    # 管理员身份由 router 级依赖保证；此处仅用于审计日志
    admin: User = Depends(require_admin),
) -> FeedbackSummaryResponse:
    """返回反馈汇总统计。

    仅统计「当前态度」：用户改赞或取消后，统计随即变化——
    评估关心的是「现在有多少回答被认为不好」，不是历史操作流水。
    """
    summary = await svc.build_summary(db, recent_limit=recent_limit or 10)
    logger.info(
        "管理员 %s 查看反馈概览：共 %d 条（赞 %d / 踩 %d）",
        admin.username, summary["total"], summary["up_count"], summary["down_count"],
    )
    return FeedbackSummaryResponse(**summary)
