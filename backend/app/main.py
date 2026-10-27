# FastAPI 应用骨架（P1-B1）
# 职责：创建应用实例、配置 CORS、注册路由、管理服务生命周期。
#
# 部署约定（见《需求清单》第四节）：nginx 反代 /api → 本服务，
# 因此这里所有路由都不带 /api 前缀（如 /chat、/health）；
# 生产由 nginx 剥离前缀后转发，本地开发可用 Vite proxy 指向 http://localhost:8000。

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import admin as admin_router
from .api import auth as auth_router
from .api import chat
from .api import conversations as conversations_router
from .api import documents as documents_router
from .config import get_settings
from .core.errors import (
    ForbiddenError,
    InvalidCredentials,
    UnauthorizedError,
    UserAlreadyExists,
    WeakPasswordError,
)
from .core.logging import get_logger, setup_logging
from .core.security import ensure_demo_user
from .schemas import HealthResponse

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子：启动时自检配置，关闭时释放资源。

    启动自检只做「提醒」不阻断启动——即使尚未填写真实 Key，
    服务也应能起来供前端联调，/chat 会返回明确的配置缺失提示。
    """
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    logger.info("启动 %s（env=%s）", settings.APP_NAME, settings.APP_ENV)
    if settings.is_deepseek_configured:
        logger.info(
            "DeepSeek 已配置：base_url=%s，model=%s",
            settings.DEEPSEEK_BASE_URL,
            settings.DEEPSEEK_MODEL,
        )
    else:
        logger.warning(
            "DeepSeek API Key 未配置或仍是占位值，/chat 将返回配置缺失提示。"
            "请在 backend/.env 中填写真实的 DEEPSEEK_API_KEY。"
        )
    logger.info("允许的前端来源（CORS）：%s", ", ".join(settings.cors_origins_list))

    # P2-B2：JWT 密钥自检。仍用内置占位密钥时任何人都能伪造令牌，必须醒目告警。
    if settings.is_jwt_configured:
        logger.info(
            "JWT 已配置：algorithm=%s，有效期 %d 分钟",
            settings.JWT_ALGORITHM,
            settings.JWT_EXPIRE_MINUTES,
        )
    else:
        logger.warning(
            "⚠️ JWT_SECRET_KEY 仍是占位值或长度不足 32 字节，令牌可被伪造！"
            "请在 backend/.env 中用 `python -c \"import secrets;print(secrets.token_urlsafe(48))\"` 生成后填入。"
        )

    # P2-B2：鉴权开关提示。AUTH_REQUIRED=false 时所有接口回退 Demo 用户（仅本地联调）。
    if settings.AUTH_REQUIRED:
        logger.info("鉴权已开启（AUTH_REQUIRED=true）：所有业务接口需携带有效 JWT")
        # 开启鉴权后身份由令牌决定，不再需要为外键预置 Demo 用户
    else:
        logger.warning(
            "⚠️ AUTH_REQUIRED=false：已关闭鉴权，所有请求回退 Demo 用户。"
            "仅用于本地无库联调，生产环境务必置为 true！"
        )
        # 仅在关闭鉴权时预置 Demo 用户，保证 conversations.user_id 外键有合法值
        await ensure_demo_user()

    yield  # 应用运行中

    logger.info("服务关闭")


def create_app() -> FastAPI:
    """应用工厂：构造并返回 FastAPI 实例。

    使用工厂模式便于单测中按需创建独立实例（配合依赖覆盖）。
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        description="知识库问答网站后端（P1 地基 MVP）：提供 SSE 流式对话能力。",
        lifespan=lifespan,
        # 生产环境关闭交互文档，减少暴露面
        docs_url="/docs" if settings.APP_ENV == "development" else None,
        redoc_url=None,
    )

    # ---------- CORS ----------
    # 本地开发时前端（Vite 5173）与后端（8000）不同源，必须放开跨域；
    # 注意：allow_origins 含 "*" 时浏览器不允许携带凭证，故此处显式列出来源。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------- 全局异常处理器（P2-B2：鉴权领域异常 → HTTP 状态码） ----------
    # 统一在这里映射，避免每个路由重复写 try/except。
    # 401 响应按 RFC 6750 带 WWW-Authenticate 头，便于前端/网关识别需重新登录。

    @app.exception_handler(UnauthorizedError)
    async def _unauthorized_handler(request: Request, exc: UnauthorizedError):
        """认证失败（令牌缺失/无效/过期/账号禁用）→ 401。"""
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(ForbiddenError)
    async def _forbidden_handler(request: Request, exc: ForbiddenError):
        """已认证但权限不足 → 403（P3 RBAC 使用）。"""
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(UserAlreadyExists)
    async def _user_exists_handler(request: Request, exc: UserAlreadyExists):
        """注册冲突（用户名/邮箱已占用）→ 409。"""
        # field 告知前端冲突发生在哪个输入框，便于高亮提示
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "field": exc.field}
        )

    @app.exception_handler(InvalidCredentials)
    async def _invalid_credentials_handler(request: Request, exc: InvalidCredentials):
        """登录失败 → 401（文案统一，不区分用户不存在与密码错误）。"""
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(WeakPasswordError)
    async def _weak_password_handler(request: Request, exc: WeakPasswordError):
        """密码强度不足 → 400，message 含具体规则供前端展示。"""
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ---------- 路由 ----------
    # P2-B2：/auth（注册 / 登录 / 当前用户）—— 唯一不需要鉴权的业务路由
    app.include_router(auth_router.router)
    # P1-B1：/chat（GET 供 EventSource，POST 供后续长文本/历史场景）
    app.include_router(chat.router)
    # P2-B1：/conversations（列表/新建/历史/软删除）
    app.include_router(conversations_router.router)
    # P1-B2：/documents/upload（文档上传：格式/大小校验 + 对象存储 + 落库元数据）
    app.include_router(documents_router.router)
    # P3-B1：/admin/*（审核队列 / 通过驳回 / 预览）；路由级 require_admin 保证仅管理员可访问
    app.include_router(admin_router.router)

    @app.get("/health", response_model=HealthResponse, tags=["ops"], summary="健康检查")
    async def health() -> HealthResponse:
        """健康检查接口：供 Docker Compose / nginx 冒烟测试使用。

        llm_ready 为 False 表示未配置 DeepSeek Key——服务本身健康，但无法真实回答。
        """
        cfg = get_settings()
        return HealthResponse(
            # 无外部依赖（DB/Redis 尚未接入），进程在即视为 ok
            status="ok",
            app=cfg.APP_NAME,
            env=cfg.APP_ENV,
            llm_ready=cfg.is_deepseek_configured,
        )

    return app


# uvicorn 入口：uvicorn app.main:app --reload
app = create_app()
