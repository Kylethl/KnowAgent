# 配置管理（P1-B1）
# 职责：集中管理后端全部可配置项，统一从环境变量 / .env 文件读取，
#       避免配置散落在业务代码中；业务模块一律通过 get_settings() 获取配置。
#
# 读取优先级（pydantic-settings 约定）：环境变量 > .env 文件 > 代码默认值

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """后端运行配置。

    所有字段均可通过同名环境变量覆盖，例如：
        export DEEPSEEK_API_KEY=sk-xxxx
        export CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
    """

    # pydantic-settings 的模型配置：指定 .env 文件、编码与大小写不敏感
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 服务基础 ----------
    # 应用名称，用于 OpenAPI 文档展示
    APP_NAME: str = "KnowAgent API"
    # 运行环境：development / production，影响文档暴露与日志级别
    APP_ENV: str = "development"
    # 服务监听地址与端口（uvicorn 启动时使用）
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    # 是否开启本地热重载（仅开发环境使用）
    RELOAD: bool = True

    # ---------- CORS ----------
    # 允许跨域的前端来源，逗号分隔。
    # P1 本地开发：Vite 默认 5173；生产由 nginx 同域托管，可按需清空。
    # 注：这里刻意用 str 而非 List[str]——pydantic-settings 对复杂类型按 JSON 解析，
    #     写成逗号分隔字符串会解析失败；由 cors_origins_list 属性负责切分。
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000"
    # 是否允许携带凭证（Cookie / Authorization 头），P2 接入鉴权后置 True
    CORS_ALLOW_CREDENTIALS: bool = True

    # ---------- DeepSeek 大模型 ----------
    # DeepSeek API Key（OpenAI 兼容协议）。必填，缺失时 /chat 会返回明确报错。
    # 注意：仓库中的 .env 仅存放占位假 Key，真实 Key 由部署方填写，不入库。
    DEEPSEEK_API_KEY: str = ""
    # DeepSeek 服务地址（OpenAI 兼容）
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    # 对话生成模型：按《需求清单》锁定为 deepseek-flash（已与 DeepSeek 在线 API 对齐）
    DEEPSEEK_MODEL: str = "deepseek-flash"
    # 采样温度：问答场景偏低，保证回答稳定
    DEEPSEEK_TEMPERATURE: float = 0.3
    # 单次回答最大 token 数，防止超长输出拖垮连接
    DEEPSEEK_MAX_TOKENS: int = 2048
    # 上游调用超时（秒）：连接超时与读取超时
    DEEPSEEK_TIMEOUT: float = 60.0

    # ---------- 系统提示词 ----------
    # P1 阶段对话尚未接入 RAG，先以通用助手身份回答；P4 接检索增强后在此追加约束
    SYSTEM_PROMPT: str = (
        "你是「知识库问答」网站的智能助手，使用简洁、准确的中文回答用户问题。"
        "若你不掌握确定信息，请如实说明，不要编造。"
    )

    # ---------- 会话记忆（P1 临时实现） ----------
    # 单会话保留的历史轮次（1 轮 = 1 条用户消息 + 1 条助手消息）
    # 说明：P1-B1 先用进程内内存实现多轮上下文；P1-A1 改由 LangChain memory 接管，
    #      P2 再落到 PostgreSQL 的 messages 表持久化。
    MEMORY_MAX_TURNS: int = 10

    # ---------- 日志 ----------
    LOG_LEVEL: str = "INFO"

    @property
    def cors_origins_list(self) -> List[str]:
        """把逗号分隔的 CORS_ORIGINS 解析为来源列表（空值返回空列表）。"""
        return [item.strip() for item in self.CORS_ORIGINS.split(",") if item.strip()]

    @property
    def is_deepseek_configured(self) -> bool:
        """判断 DeepSeek 是否已配置可用 Key（用于启动自检与友好报错）。"""
        key = (self.DEEPSEEK_API_KEY or "").strip()
        # 占位假 Key（见 .env.example）同样视为未配置，避免误判为可用
        return bool(key) and not key.startswith("sk-REPLACE_ME")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局唯一的配置实例。

    使用 lru_cache 做进程内单例，避免每次请求重复解析 .env。
    单测中需要覆盖配置时，可先执行 get_settings.cache_clear()。
    """
    return Settings()
