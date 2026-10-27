# 数据库引擎与会话（P1-DB1）
# ---------------------------------------------------------------------
# 职责：基于配置创建异步 SQLAlchemy 引擎与会话工厂，并提供 FastAPI
#       依赖 get_db 与探活函数 ping。
#
# 驱动：asyncpg（PostgreSQL 专用异步驱动），连接串形如
#      postgresql+asyncpg://<user>:<pass>@<host>:<port>/<dbname>
#      该连接串由 app.config.Settings.DATABASE_URL 提供。

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。

    集中定义元数据，便于通过 Base.metadata 统一管理建表与迁移，
    也方便后续 Alembic 等迁移工具接入。
    """


def _build_engine() -> AsyncEngine:
    """根据配置构造异步数据库引擎。

    - echo：开发期打印 SQL，生产务必关闭（见 config.DB_ECHO）。
    - pool_pre_ping：每次取连接前探活，避免使用已断开的连接。
    - future：启用 SQLAlchemy 2.x 统一行为。
    注意：此处仅创建引擎对象，不会立即建立真实连接。
    """
    settings = get_settings()
    return create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DB_ECHO,
        pool_pre_ping=True,
        future=True,
    )


# 全局异步引擎：模块导入时即创建（懒连接，不会真正连库）
engine: AsyncEngine = _build_engine()

# 异步会话工厂
#   - expire_on_commit=False：提交后对象属性不过期，便于接口直接序列化返回
#   - autoflush=False：显式控制刷写时机，避免意外触发额外查询
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：为每个请求提供一个异步会话，用完自动关闭。

    Usage:
        @app.get("/x")
        async def x(db: AsyncSession = Depends(get_db)): ...
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            # 发生异常时回滚，保证连接干净地归还连接池
            await session.rollback()
            raise


async def ping() -> bool:
    """探活：尝试建立一条连接并执行 SELECT 1，成功返回 True。

    用于启动自检或运维排查；任何连接异常都被吞掉，仅返回布尔结果，
    不向上抛错，避免探活失败阻断启动流程。
    """
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 —— 探活需吞掉所有异常，仅返回布尔
        logger.warning("数据库探活失败：%s", exc)
        return False
