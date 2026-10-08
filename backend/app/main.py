# FastAPI 应用骨架（P1-B1）
# 职责：创建应用实例、配置 CORS、注册路由、管理服务生命周期。
#
# 部署约定（见《需求清单》第四节）：nginx 反代 /api → 本服务，
# 因此这里所有路由都不带 /api 前缀（如 /chat、/health）；
# 生产由 nginx 剥离前缀后转发，本地开发可用 Vite proxy 指向 http://localhost:8000。

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import chat
from .config import get_settings
from .core.logging import get_logger, setup_logging
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

    # ---------- 路由 ----------
    # P1-B1：/chat（GET 供 EventSource，POST 供后续长文本/历史场景）
    app.include_router(chat.router)

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
