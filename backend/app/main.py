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
from .api import feedback as feedback_router
from .api import notifications as notifications_router
from .api import ops as ops_router
from .config import get_settings
from .core import metrics, sentry
from .core.errors import (
    DataResidencyViolation,
    ForbiddenError,
    InvalidCredentials,
    RateLimitExceeded,
    UnauthorizedError,
    UserAlreadyExists,
    WeakPasswordError,
)
from .core.logging import get_logger, setup_logging
from .core.middleware import ObservabilityMiddleware
from .core.security import ensure_demo_user
from .db.engine import ping as db_ping
from .schemas import HealthResponse
from .services import media_extract

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子：启动时自检配置，关闭时释放资源。

    启动自检只做「提醒」不阻断启动——即使尚未填写真实 Key，
    服务也应能起来供前端联调，/chat 会返回明确的配置缺失提示。
    """
    settings = get_settings()
    # P7-B1：结构化日志。JSON 格式供生产采集，text 供本地阅读；
    # 可选落盘 + 按大小轮转（容器部署通常留空，交给日志驱动收集）。
    setup_logging(
        level=settings.LOG_LEVEL,
        fmt=settings.LOG_FORMAT,
        file_path=settings.log_file_abs,
        max_bytes=settings.LOG_MAX_BYTES,
        backup_count=settings.LOG_BACKUP_COUNT,
    )

    # P7-B1：错误上报（DSN 为空则是 no-op，不产生任何外发流量）
    sentry.init_sentry(settings)
    # P7-Dep1：应用信息指标（Grafana 上可按版本/环境过滤）
    metrics.set_app_info("0.1.0", settings.APP_ENV)

    logger.info("启动 %s（env=%s）", settings.APP_NAME, settings.APP_ENV)
    logger.info(
        "可观测性：log_format=%s，log_file=%s，sentry=%s，metrics=%s（后端 %s）",
        settings.LOG_FORMAT,
        settings.log_file_abs or "(仅 stdout)",
        "enabled" if sentry.is_enabled() else "disabled",
        "enabled" if settings.METRICS_ENABLED else "disabled",
        metrics.backend_name(),
    )
    # P7-B1：限流与配额自检（关闭时明确告警——生产环境关闭意味着成本不受控）
    if settings.LLM_RATE_LIMIT_ENABLED:
        logger.info(
            "LLM 限流已启用：%d 次/分钟、%d 次/日、%s token/日（Redis=%s）",
            settings.LLM_RATE_LIMIT_PER_MINUTE,
            settings.LLM_RATE_LIMIT_PER_DAY,
            settings.LLM_DAILY_TOKEN_QUOTA or "不限",
            settings.rate_limit_redis_url,
        )
    else:
        logger.warning("⚠️ LLM 限流已关闭（LLM_RATE_LIMIT_ENABLED=false）：成本与滥用风险不受控")
    # P7-B2：数据不出域模式自检
    if settings.is_strict_residency:
        logger.info(
            "数据不出域：strict 模式，允许出站域名=%s",
            ", ".join(settings.outbound_allowed_hosts_list) or "(仅内网)",
        )
    else:
        logger.info("数据不出域：standard 模式（仅记录出站目标，不拦截）")
    # P7-DB1：备份能力自检
    if settings.BACKUP_ENABLED:
        logger.info(
            "备份已启用：目录=%s，保留 %d 天 / %d 份，每日 %02d:00 执行",
            settings.backup_dir_abs,
            settings.BACKUP_RETENTION_DAYS,
            settings.BACKUP_RETENTION_COUNT,
            settings.BACKUP_SCHEDULE_HOUR,
        )
    else:
        logger.warning("备份未启用（BACKUP_ENABLED=false）：数据库无自动备份")

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

    # ---------- 可观测性中间件（P7-B1） ----------
    # 放在 CORS 之后添加 → 成为最外层：连预检请求与 4xx 也会被记录，
    # 且 request_id 在进入业务代码前就已就绪（日志/审计都能带上）。
    app.add_middleware(ObservabilityMiddleware)

    # ---------- 全局异常处理器（P7：限流 / 数据不出域） ----------

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited_handler(request: Request, exc: RateLimitExceeded):
        """LLM 配额超限 → 429，并带 Retry-After 让客户端知道何时可重试。

        Retry-After 是 HTTP 标准头，浏览器/前端库都能直接读取；
        同时也把可读文案放进 detail，前端可直接展示给用户。
        """
        logger.warning(
            "请求被限流：path=%s，scope=%s，limit=%s，retry_after=%s",
            request.url.path,
            exc.scope,
            exc.limit,
            exc.retry_after,
        )
        return JSONResponse(
            status_code=429,
            content={
                "detail": exc.message,
                "scope": exc.scope,
                "limit": exc.limit,
                "retry_after": exc.retry_after,
            },
            headers={"Retry-After": str(max(1, int(exc.retry_after)))},
        )

    @app.exception_handler(DataResidencyViolation)
    async def _residency_handler(request: Request, exc: DataResidencyViolation):
        """数据不出域拦截 → 503（能力因合规策略不可用，非客户端错误）。

        用 503 而不是 403：这不是「你没权限」，而是「本部署策略不允许该能力」，
        运维看到 503 会去查配置，用户看到的提示也能明确指向合规策略。
        """
        logger.error("数据不出域拦截：%s", exc.message)
        return JSONResponse(
            status_code=503,
            content={
                "detail": exc.message,
                "host": exc.host,
                "purpose": exc.purpose,
            },
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
    # P5：/admin/documents/{id}/media（媒体原文件流式预览）
    # 单独注册的原因：该接口需支持 ?token= 传参（浏览器媒体标签无法带请求头），
    # 因此不能挂在「只认请求头」的路由级 require_admin 之下。
    app.include_router(admin_router.media_router)
    # P6-B1：/notifications（站内信：列表 / 未读数 / 标记已读）
    app.include_router(notifications_router.router)
    # P6-B2：/messages/{id}/feedback（答案反馈：点赞 / 点踩 / 取消）
    app.include_router(feedback_router.router)
    # P6-B2：/admin/feedback/summary（答案质量评估概览，路由级 require_admin）
    app.include_router(feedback_router.admin_router)
    # P7：/ops/*（运维状态 / 审计查询 / 备份 / 合规自检，路由级 require_admin）
    app.include_router(ops_router.router)
    # P7：/metrics（Prometheus 抓取；不要求管理员，按 METRICS_TOKEN 可选保护）
    app.include_router(ops_router.metrics_router)

    @app.get("/health", response_model=HealthResponse, tags=["ops"], summary="健康检查")
    async def health() -> HealthResponse:
        """存活探针：只确认进程在跑，不做任何外部依赖检查。

        为什么保持「轻量」：
            Docker/K8s 会高频调用它（每 5~10 秒），一旦这里去连数据库/Redis，
            依赖抖动会让容器被误杀重启。依赖检查放在 /health/ready 与 /ops/status。

        llm_ready 为 False 表示未配置 DeepSeek Key——服务本身健康，但无法真实回答。
        """
        cfg = get_settings()
        caps = media_extract.capabilities()
        return HealthResponse(
            # 无外部依赖（DB/Redis 尚未接入），进程在即视为 ok
            status="ok",
            app=cfg.APP_NAME,
            env=cfg.APP_ENV,
            llm_ready=cfg.is_deepseek_configured,
            # P5：多模态解析能力（图片 OCR / 音视频 ASR），便于部署后一眼确认是否可用
            media_ocr_ready=bool(caps["ocr"]),
            media_asr_ready=bool(caps["asr"]),
            media_ffmpeg=bool(caps["ffmpeg"]),
        )

    @app.get("/health/ready", tags=["ops"], summary="就绪检查（依赖探活）")
    async def readiness() -> JSONResponse:
        """就绪探针：数据库可用才返回 200，否则 503。

        用途：
            - K8s readinessProbe：未就绪的 Pod 不接流量（避免 500 打到用户）；
            - 部署脚本的「等服务起来」判断。

        说明：只检查**关键路径依赖**（数据库）。Redis/对象存储属于可降级依赖，
        它们不可用时系统仍能对话（缓存失效、入库排队），因此不阻断就绪。
        """
        db_ok = await db_ping()
        payload = {
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "down",
        }
        return JSONResponse(status_code=200 if db_ok else 503, content=payload)

    return app


# uvicorn 入口：uvicorn app.main:app --reload
app = create_app()
