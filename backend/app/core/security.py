# 鉴权占位实现（P2 前置）
# ---------------------------------------------------------------------
# 职责：P2 功能1（对话历史与删除）需要「会话/消息按 user_id 隔离」，但真正的登录鉴权
#       （P2-B2：JWT + 注册/登录 + bcrypt）尚未在本轮开发。为避免阻塞 P2-F1/B1，
#       这里提供一个**占位**的当前用户解析逻辑：
#         - 优先取请求头 X-User-Id（便于后续 P2-B2 登录后由前端携带真实用户 ID）；
#         - 缺失时回退到一个启动时预置的「Demo 用户」，保证 conversations.user_id
#           外键始终有合法值，使历史/删除功能可端到端联调。
#
# ⚠️ 替换约定：P2-B2 落地后，get_current_user_id 应改为「校验 JWT → 解出用户 ID」，
#       并移除 Demo 用户回退（或仅保留为本地开发默认）。本文件的所有接口契约
#       （返回 UUID）保持不变，上层路由与前端无需改动。
#
# 说明：演示环境不要求真实登录，前端暂不在 localStorage 维护 user 概念；
#       后端自动落到 Demo 用户即可。多用户隔离将在 P2-B2 接入真实令牌后生效。

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import Header
from sqlalchemy import select

from ..core.logging import get_logger
from ..db.engine import SessionLocal
from ..db.models import User

logger = get_logger(__name__)

# Demo 用户：使用固定 UUID，保证多次启动一致、便于前端/测试引用。
# 用户名固定为 'demo'；密码哈希为占位（P2-B2 会改成真实 bcrypt 值）。
DEMO_USERNAME = "demo"
DEMO_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


async def ensure_demo_user() -> UUID:
    """启动时确保 Demo 用户存在（幂等 upsert）。

    为什么需要它：
        conversations.user_id 是 NOT NULL 外键（引用 users.id），若用户表为空，
        任何会话写入都会因外键约束失败。本函数在应用启动时插入一个固定 ID 的
        Demo 用户，使 P2-F1/B1 在「无登录」阶段也能正常落库与隔离。

    容错：若数据库暂不可达，仅记录警告并返回固定 ID，不阻断应用启动
          （与 main.py lifespan 的「自检不阻断」原则一致）。

    Returns:
        Demo 用户的 UUID（固定值）。
    """
    try:
        async with SessionLocal() as session:
            # 先查后插，避免重复主键冲突（幂等）
            existing = await session.execute(
                select(User).where(User.id == DEMO_USER_ID)
            )
            if existing.scalar_one_or_none() is None:
                session.add(
                    User(
                        id=DEMO_USER_ID,
                        username=DEMO_USERNAME,
                        # 占位密码哈希；P2-B2 前不会被用于登录校验
                        password_hash="__demo_placeholder__",
                        role="user",
                    )
                )
                await session.commit()
                logger.info("已创建 Demo 用户（id=%s）用于 P2 历史功能联调", DEMO_USER_ID)
            else:
                logger.debug("Demo 用户已存在，跳过创建：id=%s", DEMO_USER_ID)
    except Exception as exc:  # noqa: BLE001 —— 启动自检容错，不阻断启动
        logger.warning("Demo 用户预置失败（数据库可能未就绪）：%s", exc)
    return DEMO_USER_ID


def resolve_user_id(x_user_id: Optional[str]) -> UUID:
    """把请求中的用户标识解析为 UUID。

    规则：
      - 若请求头 X-User-Id 提供了合法的 UUID，则直接使用（P2-B2 后由登录令牌下发）；
      - 否则回退到 Demo 用户（无登录阶段的默认行为）。

    注意：此处**不做鉴权**，仅做用户解析。P2-B2 会在此处替换成「校验 JWT 并解出
          user_id」的逻辑，且对非法/缺失令牌直接拒绝（401）。

    Args:
        x_user_id: 请求头 X-User-Id 的值（可能为 None 或非 UUID 字符串）。

    Returns:
        当前请求归属的用户 UUID。
    """
    if x_user_id:
        try:
            return UUID(x_user_id.strip())
        except (ValueError, AttributeError):
            # 非法 UUID：不信任，回退 Demo 用户（P2-B2 这里应改为 401 拒绝）
            logger.debug("X-User-Id 非法（%s），回退 Demo 用户", x_user_id)
    return DEMO_USER_ID


async def get_current_user_id(
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> UUID:
    """FastAPI 依赖：解析当前用户 ID（占位实现，P2-B2 将替换为 JWT 校验）。

    Usage:
        user_id: UUID = Depends(get_current_user_id)
    """
    return resolve_user_id(x_user_id)
