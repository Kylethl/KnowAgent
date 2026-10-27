# 鉴权与当前用户解析（P2-B2：JWT 正式实现）
# ---------------------------------------------------------------------
# 职责：提供 FastAPI 鉴权依赖，把「请求 → 当前用户」的解析收敛到一处：
#         - get_current_user        ：校验 JWT 并返回完整 User 对象（推荐）
#         - get_current_user_id     ：只返回用户 UUID（会话/文档等按 user_id 隔离时用）
#         - require_admin           ：RBAC 管理员校验（P3 管理后台用，本阶段预留）
#         - get_user_for_request    ：非依赖场景（如 SSE 生成器内部）手动解析用户
#
# 变更说明（P2 功能2）：
#   本文件此前是「占位实现」——从 X-User-Id 头取用户、缺失时回退 Demo 用户，
#   目的是让 P2 功能1（对话历史）在无登录状态下也能联调。
#   现在 P2-B2 落地真实鉴权，改为：
#     1. 从标准 Authorization: Bearer <JWT> 头取令牌；
#     2. 校验签名与有效期，反查用户并校验账号启用状态；
#     3. 失败抛 UnauthorizedError → 由 main.py 的全局异常处理器映射为 401。
#   X-User-Id 头已废弃（不再信任客户端自报身份），仅保留 Demo 回退开关
#   AUTH_REQUIRED=false 供本地无库联调，生产必须保持 true（P2-Dep1）。
#
# 安全基线：
#   - 绝不信任客户端传入的用户 ID，身份一律由令牌签名推导；
#   - 令牌无效/过期/用户被禁用一律 401，前端收到后清 token 并跳登录页。

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..core.errors import ForbiddenError, UnauthorizedError
from ..core.logging import get_logger
from ..db.engine import SessionLocal, get_db
from ..db.models import User
from ..services import auth_service

logger = get_logger(__name__)

# ---------------------------------------------------------------------
# 本地开发回退用的 Demo 用户（AUTH_REQUIRED=false 时启用）
# ---------------------------------------------------------------------
# 使用固定 UUID，保证多次启动一致、便于前端/测试引用。
# 密码哈希为不可匹配的占位值，因此 Demo 用户**无法通过密码登录**，
# 只能作为「关闭鉴权时」的匿名兜底身份，避免被误当成真实账号使用。
DEMO_USERNAME = "demo"
DEMO_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

# Bearer 令牌提取器：auto_error=False 让我们自己控制 401 的文案与行为，
# 而不是由 FastAPI 直接抛出默认的 "Not authenticated"。
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT 访问令牌（Bearer）")


async def _ensure_demo_user_in_session(db: AsyncSession) -> User:
    """在给定会话内确保 Demo 用户存在并返回（幂等）。

    与 ensure_demo_user() 的区别：本函数复用调用方已有的会话，
    适合在请求链路内使用；后者自带会话，适合应用启动时调用。

    Args:
        db: 数据库会话（不负责提交，事务边界由调用方控制）。

    Returns:
        Demo 用户对象。
    """
    user = await auth_service.get_user_by_id(db, DEMO_USER_ID)
    if user is None:
        user = User(
            id=DEMO_USER_ID,
            username=DEMO_USERNAME,
            # 占位哈希：不是合法 bcrypt 串，因此该账号无法通过密码登录
            password_hash="__demo_placeholder__",
            role="user",
        )
        db.add(user)
        # flush 拿到主键，便于同一事务内立即关联写入
        await db.flush()
        logger.debug("会话内已创建 Demo 用户（id=%s）", DEMO_USER_ID)
    return user


async def ensure_demo_user() -> UUID:
    """应用启动时确保 Demo 用户存在（幂等 upsert），返回其固定 UUID。

    为什么还需要它：
        本地把 AUTH_REQUIRED 设为 false 做无登录联调时，请求会落到 Demo 用户，
        而 conversations.user_id 是引用 users(id) 的外键，用户不存在会写库失败。
        本函数在启动时预置该用户，保证外键始终有合法值。

    容错：数据库暂不可达时仅记录警告并返回固定 ID，不阻断应用启动
          （与 main.py lifespan「自检不阻断」原则一致）。

    Returns:
        Demo 用户的 UUID（固定值）。
    """
    try:
        async with SessionLocal() as session:
            await _ensure_demo_user_in_session(session)
            await session.commit()
            logger.info("Demo 用户就绪（id=%s），供 AUTH_REQUIRED=false 本地联调使用", DEMO_USER_ID)
    except Exception as exc:  # noqa: BLE001 —— 启动自检容错，不阻断启动
        logger.warning("Demo 用户预置失败（数据库可能未就绪）：%s", exc)
    return DEMO_USER_ID


def extract_bearer_token(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    """从 HTTPBearer 解析结果中取出裸令牌字符串。

    Args:
        credentials: HTTPBearer 解析结果；未携带 Authorization 头时为 None。

    Returns:
        令牌字符串；无令牌时返回 None。
    """
    return credentials.credentials if credentials else None


async def get_user_for_request(db: AsyncSession, token: Optional[str]) -> User:
    """按令牌解析当前用户（供非依赖场景使用，如 SSE 事件生成器内部）。

    解析规则：
      - AUTH_REQUIRED=false（本地联调开关）：忽略令牌，直接回退 Demo 用户；
      - AUTH_REQUIRED=true（默认/生产）：令牌必填，缺失或无效抛 UnauthorizedError。

    Args:
        db: 数据库会话。
        token: 裸 JWT 字符串（不含 'Bearer ' 前缀）；可为 None。

    Returns:
        当前用户对象。

    Raises:
        UnauthorizedError: 需要鉴权但令牌缺失 / 无效 / 过期 / 账号被禁用。
    """
    cfg = get_settings()

    # 本地无鉴权联调：回退 Demo 用户身份
    if not cfg.AUTH_REQUIRED:
        return await _ensure_demo_user_in_session(db)

    if not token:
        raise UnauthorizedError("缺少登录令牌，请先登录")

    return await auth_service.get_user_from_token(db, token)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI 依赖：解析当前登录用户（JWT 鉴权，P2-B2 正式实现）。

    Usage:
        @app.get("/x")
        async def x(user: User = Depends(get_current_user)): ...

    说明：本依赖与路由中的 Depends(get_db) 用的是同一个 callable，
          FastAPI 会在单请求内缓存复用同一个会话，不会重复开连接。

    Raises:
        UnauthorizedError: 令牌缺失 / 无效 / 过期 / 用户被禁用（→ 401）。
    """
    token = extract_bearer_token(credentials)
    return await get_user_for_request(db, token)


async def get_current_user_id(
    user: User = Depends(get_current_user),
) -> UUID:
    """FastAPI 依赖：只取当前用户的 UUID（历史接口沿用此签名）。

    刻意保持与 P2 功能1 相同的依赖名与返回类型，
    使 /conversations 等路由无需改动即自动升级为真实鉴权。

    Returns:
        当前登录用户的 UUID。
    """
    return user.id


async def get_current_user_id_sse(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    token: Optional[str] = Query(
        default=None,
        description="JWT 访问令牌（EventSource 无法自定义请求头时的备用传参方式）",
    ),
    db: AsyncSession = Depends(get_db),
) -> UUID:
    """SSE 场景专用鉴权依赖：同时支持 Authorization 头与 query 传令牌。

    为什么需要它：
        《需求清单》把流式协议锁定为 SSE（EventSource），而浏览器原生
        EventSource **无法自定义请求头**，因此前端无法在 /chat 上携带
        Authorization。令牌只能退而求其次走 query 参数。

    安全权衡（已在 P2-F2 采用）：
        - 令牌出现在 URL 中，可能被代理/访问日志、浏览器历史记录留存；
        - 缓解措施：① P2-Dep1 全站 HTTPS，URL 路径在传输中加密；
                   ② 令牌有效期有限（默认 1 天），泄露窗口可控；
                   ③ 后端不把 token 写入日志（见 api/chat.py 日志脱敏）。
        - 更彻底的方案是改用 fetch + ReadableStream 消费 POST /chat（可带请求头），
          但会偏离「SSE + EventSource」的既定选型，故本阶段保留 query 方式。

    Returns:
        当前登录用户的 UUID。

    Raises:
        UnauthorizedError: 令牌缺失 / 无效 / 过期 / 用户被禁用（→ 401）。
    """
    # 优先用标准请求头（fetch 场景），缺失时回退 query（EventSource 场景）
    raw_token = extract_bearer_token(credentials) or token
    user = await get_user_for_request(db, raw_token)
    return user.id


async def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """FastAPI 依赖：要求当前用户为管理员（RBAC，为 P3 管理后台预留）。

    《需求清单》P2-B2 要求 users 表预留 role（user/admin）；
    本依赖即该权限体系的落点，P3-B1 的 /admin/* 接口直接依赖它即可。

    Raises:
        ForbiddenError: 已登录但 role != 'admin'（→ 403）。
    """
    if user.role != "admin":
        logger.warning("越权访问被拦截：user=%s，role=%s", user.username, user.role)
        raise ForbiddenError("需要管理员权限")
    return user
