# 对话历史与删除接口（P2-B1）
# ---------------------------------------------------------------------
# 职责：提供会话维度的 CRUD，支撑前端 P2-F1「左侧会话列表（新建/选中/删除）」。
#   约定路由（与《开发任务清单》P2-B1 对齐）：
#     GET    /conversations            会话列表（按 user_id 隔离，排除软删除）
#     POST   /conversations            新建会话
#     GET    /conversations/{id}       会话历史（消息按时间正序）
#     DELETE /conversations/{id}       软删除（标记 deleted_at，不物理删除）
#
# 隔离与鉴权：
#   - 当前用户由 core/security.get_current_user_id 解析。
#     P2 功能1 时它是「X-User-Id 占位 + Demo 用户回退」，P2-B2 落地真实鉴权后，
#     该依赖已升级为「校验 JWT → 解出用户」，本文件无需改动即自动获得真实身份；
#   - 每个查询都带 user_id 过滤，天然实现「会话/消息按 user_id 隔离」（P2-DB1）；
#   - 越权访问统一返回 404（不区分「不存在」与「不属于你」）。
#
# 注意：本文件不引入 /api 前缀，由 nginx/Vite 代理统一剥离前缀后转发（同 /chat）。

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from ..core.errors import ConversationNotFound
from ..core.logging import get_logger
from ..core.security import get_current_user_id
from ..db.engine import get_db
from ..db.models import Message
from ..schemas import (
    ConversationCreate,
    ConversationDetail,
    ConversationMessageOut,
    ConversationOut,
    ConversationSummary,
)
from ..services import conversation_service as svc

logger = get_logger(__name__)

# 路由集合；main.py 中以 /conversations 前缀相关路径统一注册
router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationSummary], summary="会话列表")
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> list[ConversationSummary]:
    """获取当前用户的会话列表（排除已软删除），按最近活跃倒序。

    每个列表项附带 message_count，便于前端展示「最近一条 / 条数」预览。
    """
    conversations = await svc.list_conversations(db, user_id)

    # 批量统计每个会话的消息条数（一次查询，避免 N+1）
    ids = [c.id for c in conversations]
    counts: dict[UUID, int] = {}
    if ids:
        res = await db.execute(
            select(Message.conversation_id, func.count(Message.id))
            .where(Message.conversation_id.in_(ids))
            .group_by(Message.conversation_id)
        )
        # 注意：协程的是 db.execute()（已 await），返回的 Result 是同步对象，
        #       因此 Result.all() 直接同步调用即可；对它再加 await 会抛
        #       "TypeError: object list can't be used in 'await' expression"。
        rows = res.all()
        counts = {row[0]: row[1] for row in rows}

    return [
        ConversationSummary(
            id=str(c.id),
            title=c.title,
            model=c.model,
            created_at=c.created_at.isoformat() if c.created_at else "",
            updated_at=c.updated_at.isoformat() if c.updated_at else "",
            message_count=counts.get(c.id, 0),
        )
        for c in conversations
    ]


@router.post(
    "",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建会话",
)
async def create_conversation(
    payload: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> ConversationOut:
    """新建一个会话（默认标题留空，由首条用户消息自动回填）。

    前端「新建会话」按钮调用此接口获得会话 ID，随后即可在该会话下提问/续聊。
    """
    # 先建会话（拿到 id），再按需回填标题
    conv = await svc.get_or_create_conversation(db, user_id, conversation_id=None)
    if payload.title:
        conv.title = payload.title

    # 必须无条件 commit：service 内只做了 flush（仅为了拿回生成的 UUID），
    # 若不提交，连接关闭时事务回滚，前端虽收到 201 但库中查不到该会话，
    # 表现就是「新建的会话刷新后消失、历史列表为空」。
    await db.commit()
    # commit 后由数据库生成的字段（updated_at 带 onupdate=func.now()）会被标记为过期，
    # 下面构造响应时若直接读取，会在同步序列化阶段触发隐式 IO，
    # 抛出 MissingGreenlet（greenlet_spawn has not been called）。故此处显式异步刷新。
    await db.refresh(conv)

    logger.info("已新建会话：id=%s，user_id=%s", conv.id, user_id)
    return ConversationOut(
        id=str(conv.id),
        title=conv.title,
        model=conv.model,
        created_at=conv.created_at.isoformat() if conv.created_at else "",
        updated_at=conv.updated_at.isoformat() if conv.updated_at else "",
    )


@router.get("/{conversation_id}", response_model=ConversationDetail, summary="会话历史")
async def get_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> ConversationDetail:
    """获取指定会话的历史消息（按时间正序），用于「选中会话回看 / 续聊」。

    归属校验失败的会话统一返回 404，不泄露是否存在或属于他人。
    """
    try:
        conv_uuid = UUID(conversation_id)
    except (ValueError, AttributeError):
        # ID 格式非法也按「找不到」处理，统一 404 语义
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    try:
        conv, messages = await svc.get_conversation_detail(db, user_id, conv_uuid)
    except ConversationNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权访问")

    return ConversationDetail(
        id=str(conv.id),
        title=conv.title,
        model=conv.model,
        created_at=conv.created_at.isoformat() if conv.created_at else "",
        updated_at=conv.updated_at.isoformat() if conv.updated_at else "",
        messages=[
            ConversationMessageOut(
                id=str(m.id),
                role=m.role,
                content=m.content,
                created_at=m.created_at.isoformat() if m.created_at else "",
            )
            for m in messages
        ],
    )


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话（软删除）",
)
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> None:
    """软删除会话：仅标记 deleted_at，不物理删除（满足《需求清单》软删除约定）。

    前端「删除」按钮带二次确认后调用；成功后该会话不再出现在列表与历史中。
    """
    try:
        conv_uuid = UUID(conversation_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    try:
        await svc.soft_delete_conversation(db, user_id, conv_uuid)
    except ConversationNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权访问")

    # 204：无响应体（软删除成功即返回）
    return None
