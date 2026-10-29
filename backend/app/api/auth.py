# 用户注册 / 登录 / 当前用户信息（P2-B2）
# ---------------------------------------------------------------------
# 职责：提供账号体系入口，支撑前端 P2-F2「登录/注册页 + token 存储 + 路由守卫」。
#   约定路由（与《开发任务清单》P2-B2 对齐）：
#     POST /auth/register   注册（密码 bcrypt 加密后落库）
#     POST /auth/login      登录（校验密码，签发 JWT）
#     GET  /auth/me         获取当前登录用户（鉴权依赖校验令牌）
#
# 设计要点：
#   - 注册接口**不接受** role 字段：角色一律为 'user'，杜绝客户端提权；
#     admin 由运维在数据库直接授权，P3 管理后台再用 require_admin 校验。
#   - 登录失败统一 401 且不区分「用户名不存在 / 密码错误」，防止用户名枚举。
#   - 所有业务异常抛 core.errors 中的领域异常，由 main.py 的全局处理器
#     统一映射为 HTTP 状态码（401/403/409/400），路由层不散落 try/except。
#
# 注意：本文件不引入 /api 前缀，由 nginx/Vite 代理统一剥离前缀后转发（同 /chat）。

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import InvalidCredentials, UserAlreadyExists, WeakPasswordError
from ..core.logging import get_logger
from ..core.security import get_current_user
from ..db.engine import get_db
from ..db.models import User
from ..schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from ..services import audit_service, auth_service

logger = get_logger(__name__)

# 路由集合；main.py 中以 /auth 前缀统一注册
router = APIRouter(prefix="/auth", tags=["auth"])


def _to_user_out(user: User) -> UserOut:
    """把 ORM 用户对象转换为对外响应模型（脱敏）。

    单独抽出的原因：注册/登录/me 三处都要用，
    且必须显式剔除 password_hash，避免把敏感字段带进响应。

    Args:
        user: 用户 ORM 对象。

    Returns:
        UserOut 响应模型。
    """
    return UserOut(
        id=str(user.id),
        username=user.username,
        email=user.email,
        role=user.role,
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="用户注册",
)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    """注册新账号：校验密码强度 → 查重 → bcrypt 加密 → 落库。

    成功后返回脱敏用户信息（**不自动登录**）：
    前端注册成功后再调用 /auth/login 换取令牌，保持接口职责单一。

    Raises:
        WeakPasswordError: 密码强度不足（→ 400）。
        UserAlreadyExists: 用户名或邮箱已被占用（→ 409）。
    """
    try:
        # 角色不由客户端指定，服务端固定为 'user'
        user = await auth_service.register_user(
            db,
            username=payload.username,
            password=payload.password,
            email=payload.email,
            role="user",
        )
        await db.commit()
    except (WeakPasswordError, UserAlreadyExists):
        # 领域异常直接向上抛，由全局处理器映射状态码
        raise
    except IntegrityError:
        # 并发注册同一用户名/邮箱时的唯一约束冲突（竞态），转成 409 而非 500
        await db.rollback()
        logger.warning("注册触发唯一约束冲突（并发）：username=%s", payload.username)
        raise UserAlreadyExists(field="username")

    logger.info("用户注册成功：username=%s", payload.username)
    # P7-B2：注册是账号生命周期的起点，写审计（不记录密码等敏感字段）
    await audit_service.record(
        "auth.register",
        user=user,
        resource_type="user",
        resource_id=str(user.id),
        detail={"email_provided": bool(payload.email)},
    )
    return _to_user_out(user)


@router.post("/login", response_model=TokenResponse, summary="用户登录")
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """登录：校验用户名与密码，通过后签发 JWT 访问令牌。

    Returns:
        TokenResponse：access_token + 有效期 + 脱敏用户信息。

    Raises:
        InvalidCredentials: 用户名或密码错误 / 账号被禁用（→ 401，不区分具体原因）。
    """
    try:
        # 认证失败由 auth_service 抛 InvalidCredentials（统一文案，防用户名枚举）
        user = await auth_service.authenticate_user(db, payload.username, payload.password)
    except InvalidCredentials:
        # P7-B2：登录失败是安全审计里最重要的事件之一——
        # 连续失败往往意味着撞库/暴力破解，必须留痕（只记用户名，不记密码）。
        await audit_service.record(
            "auth.login_failed",
            result="failure",
            username=payload.username,
            role="anonymous",
            resource_type="user",
            detail={"reason": "invalid_credentials"},
        )
        raise

    token = auth_service.create_access_token(user)

    from ..config import get_settings  # 局部导入：避免在模块导入期就读取配置

    cfg = get_settings()
    logger.info("用户登录成功：username=%s，id=%s", user.username, user.id)
    # P7-B2：登录成功留痕（含角色，便于核查是否有人以 admin 身份进入）
    await audit_service.record(
        "auth.login",
        user=user,
        resource_type="user",
        resource_id=str(user.id),
        detail={"role": user.role},
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        # 有效期换算成秒返回，便于前端判断是否已过期
        expires_in=cfg.JWT_EXPIRE_MINUTES * 60,
        user=_to_user_out(user),
    )


@router.get("/me", response_model=UserOut, summary="当前登录用户")
async def me(
    user: User = Depends(get_current_user),
) -> UserOut:
    """获取当前登录用户信息（校验令牌有效性的轻量接口）。

    前端用途：
      1. 刷新页面时用它判断本地 token 是否仍有效；
      2. 顶栏展示用户名与角色。

    Returns:
        当前用户的脱敏信息。
    """
    return _to_user_out(user)
