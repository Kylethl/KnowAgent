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

    # ---------- 数据库（PostgreSQL，P1-DB1 起） ----------
    # 异步连接串：使用 asyncpg 驱动（SQLAlchemy 2.x 异步引擎）。
    # 格式：postgresql+asyncpg://<user>:<password>@<host>:<port>/<db>
    # 默认指向本地 Docker Compose 中的 PostgreSQL（见 P1-Dep1）。
    DATABASE_URL: str = "postgresql+asyncpg://knowagent:knowagent@localhost:5432/knowagent"
    # 是否打印 SQL（调试用，生产务必关闭）
    DB_ECHO: bool = False

    # ---------- 用户鉴权（P2-B2：JWT + bcrypt） ----------
    # JWT 签名密钥（HS256）。⚠️ 必须使用足够长（≥32 字节）的随机值，
    # 切勿提交到代码仓库；生产环境务必通过环境变量注入。
    # 生成方式：python -c "import secrets;print(secrets.token_urlsafe(48))"
    JWT_SECRET_KEY: str = "CHANGE_ME_INSECURE_DEV_SECRET"
    # JWT 签名算法：对称密钥场景固定 HS256（PyJWT 支持）
    JWT_ALGORITHM: str = "HS256"
    # 访问令牌有效期（分钟）：默认 1 天（60*24）。
    # 说明：令牌不持久化、无刷新令牌，过期后重新登录即可（P2 阶段足够）。
    JWT_EXPIRE_MINUTES: int = 60 * 24
    # 是否强制所有业务接口要求鉴权（P2-Dep1「所有接口要求鉴权」）。
    # 说明：置 False 时接口回退到「Demo 用户」模式，仅供本地无库联调；
    #       生产/联调务必保持 True。
    AUTH_REQUIRED: bool = True
    # 密码哈希强度：bcrypt cost factor。12 为安全与性能的常用折中。
    BCRYPT_ROUNDS: int = 12

    # ---------- 会话记忆（P1-A1：LangChain 记忆） ----------
    # 单会话保留的历史轮次（1 轮 = 1 条用户消息 + 1 条助手消息）。
    # 超出后由 services/langchain_memory.py 按窗口裁剪，防止上下文膨胀导致 token 溢出。
    MEMORY_MAX_TURNS: int = 10
    # 进程内同时保留的会话数量上限，超出后按 LRU 淘汰最久未使用的会话，避免长期运行内存无限增长。
    # 说明：P1 仍为进程内内存（重启即丢失、多副本不共享）；
    #      P2（P2-DB1 / P2-A1）会落 PostgreSQL 的 messages 表做持久化。
    MEMORY_MAX_SESSIONS: int = 1000
    # 注入 LangChain 的历史令牌预算（P2-DB1 上下文窗口管理）：
    # 从对话持久化消息中加载历史时，最多携带约该数量的估算 token，超出则从最旧开始丢弃，
    # 防止超长对话把整段历史塞进 prompt 导致 token 溢出 / 成本飙升。
    CONTEXT_TOKEN_BUDGET: int = 4000

    # ---------- 对象存储与文件上传（P1-B2：文档上传落盘） ----------
    # 存储后端类型：local（本地目录，开发免 MinIO）/ minio（MinIO / S3 兼容）。
    # 说明：生产按《需求清单》使用 MinIO / OSS / S3；本地开发默认 local 可零依赖跑通上传闭环，
    #      切换只需改 STORAGE_BACKEND 并填好 MinIO 凭证，业务代码不变（见 services/storage.py）。
    STORAGE_BACKEND: str = "local"
    # local 后端落盘根目录（相对 backend 目录；生产可指向挂载卷）。二进制绝不进数据库。
    STORAGE_LOCAL_DIR: str = "data/uploads"
    # MinIO / S3 兼容服务的地址（含端口），如 localhost:9000
    STORAGE_ENDPOINT: str = "localhost:9000"
    # 存储桶名（生产用独立私有桶；见《云存储测试方案》S4 私有桶约定）
    STORAGE_BUCKET: str = "knowagent"
    # 访问凭证；留空时 local 后端忽略，minio 后端会拒绝并在调用时明确报错
    STORAGE_ACCESS_KEY: str = ""
    STORAGE_SECRET_KEY: str = ""
    # 区域（可选，部分 S3 兼容服务需要）
    STORAGE_REGION: str = ""
    # 预签名 URL 有效期（秒），供后续下载/预览接口使用（P3 起）
    STORAGE_PRESIGN_EXPIRE: int = 3600
    # 单文件上传大小上限（字节）：默认 20MB，与前端 documentUpload.ts 的 MAX_FILE_SIZE_MB 一致
    MAX_UPLOAD_SIZE: int = 20 * 1024 * 1024
    # 允许上传的扩展名白名单（逗号分隔，小写）。P1 仅 Word / Markdown。
    # P5 再扩展 image/audio/video。
    ALLOWED_EXTENSIONS: str = "docx,doc,md"

    # ---------- 知识库向量与入库（P3 功能2：RAG 管线） ----------
    # 千问 Embedding 服务的 API Key（阿里云百炼 / DashScope）。
    # ⚠️ 开发期可留空：留空或形如 sk-REPLACE_ME 时，embedding 走**确定性 mock 向量**，
    #    仅用于跑通「解析 → 切片 → 写向量」管线，向量相似度无语义，不可用于真实检索。
    #    接入真实检索（P4）前必须填入真实 Key。
    QWEN_API_KEY: str = ""
    # Embedding 服务地址：qwen3-vl-embedding 走 DashScope 多模态向量接口
    QWEN_EMBEDDING_BASE_URL: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
        "multimodal-embedding/multimodal-embedding"
    )
    # 向量模型：按《需求清单》锁定为 qwen3-vl-embedding（文本/图文多模态，P5 复用）
    QWEN_EMBEDDING_MODEL: str = "qwen3-vl-embedding"
    # 向量维度：qwen3-vl-embedding 支持 2560/2048/1536/1024/768/512/256，默认 2560。
    # 本项目取 1024 权衡存储与召回；⚠️ 必须与 sql/004 的 vector(N) 与 ORM 的 EMBEDDING_DIM 一致，
    # 变更需 DROP 重建 document_chunks 表。
    EMBEDDING_DIM: int = 1024
    # 单批送入 embedding 的文本条数：qwen3-vl-embedding 单次请求的 contents 元素上限为 20，
    # 取较小值以控制单次请求体积与失败重试成本。
    EMBEDDING_BATCH_SIZE: int = 6
    # 单次 embedding 请求的超时（秒）与失败重试次数
    EMBEDDING_TIMEOUT: float = 30.0
    EMBEDDING_MAX_RETRIES: int = 2
    # Embedding 接口协议模式：auto / openai / dashscope
    #   - auto（默认）：根据 BASE_URL 自动判断——
    #       BASE_URL 含 "compatible-mode" 或以 "/v1" 结尾 → OpenAI 兼容协议（POST {base}/embeddings）；
    #       否则视为 DashScope 原生多模态向量接口（BASE_URL 已是完整 endpoint）。
    #   - openai：强制 OpenAI 兼容 embeddings 协议（阿里云百炼 compatible-mode 网关、本地 vLLM 等）。
    #   - dashscope：强制 DashScope 多模态向量接口（qwen3-vl-embedding 原生路径）。
    # 本项目本地开发配的是百炼 compatible-mode 网关，auto 即可正确识别为 openai 模式。
    QWEN_EMBEDDING_API_MODE: str = "auto"
    # 切片策略（P3-A1）：目标切片字符数与相邻切片重叠字符数。
    # 800/120 对中文文档是常用取值：既保证单切片语义完整，又避免跨切片丢失上下文。
    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 120

    # ---------- 异步任务（P3-B2：Celery + Redis） ----------
    # Celery 的消息队列与结果后端（P3-Dep1 引入 Redis）。
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    # 是否使用 Celery 执行入库任务。置 False 或未装 celery / Redis 不可达时，
    # 后端自动回退为「进程内后台任务」，保证开发期无 Redis 也能跑通入库闭环。
    USE_CELERY: bool = True

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

    @property
    def is_jwt_configured(self) -> bool:
        """判断 JWT 签名密钥是否已替换为安全随机值（P2-B2 启动自检用）。

        仍在使用内置占位密钥时视为「未配置」：此时任何人都能伪造令牌，
        必须在启动时给出醒目告警（生产环境应视为严重配置错误）。
        """
        secret = (self.JWT_SECRET_KEY or "").strip()
        # 占位密钥或长度不足 32 字节（HS256 的 RFC 7518 建议下限）均视为不安全
        return bool(secret) and secret != "CHANGE_ME_INSECURE_DEV_SECRET" and len(secret) >= 32

    @property
    def allowed_extensions_list(self) -> List[str]:
        """把逗号分隔的 ALLOWED_EXTENSIONS 解析为小写扩展名列表（去空）。

        形如 "docx,doc,md" → ["docx", "doc", "md"]。供存储校验层做白名单比对。
        """
        return [ext.strip().lower() for ext in self.ALLOWED_EXTENSIONS.split(",") if ext.strip()]

    @property
    def max_upload_size_mb(self) -> float:
        """把字节上限换算为 MB，便于日志/错误提示中展示。"""
        return round(self.MAX_UPLOAD_SIZE / (1024 * 1024), 2)

    @property
    def is_qwen_configured(self) -> bool:
        """判断千问 Embedding 是否已配置可用 Key（P3 功能2 启动自检用）。

        留空或仍为占位值（sk-REPLACE_ME）时视为未配置：
        此时 embedding 走 mock 向量，入库管线可跑通但向量不具备语义相似度，
        启动时会给出醒目告警，避免误以为已具备检索能力（P4 的前提）。
        """
        key = (self.QWEN_API_KEY or "").strip()
        return bool(key) and not key.startswith("sk-REPLACE_ME")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局唯一的配置实例。

    使用 lru_cache 做进程内单例，避免每次请求重复解析 .env。
    单测中需要覆盖配置时，可先执行 get_settings.cache_clear()。
    """
    return Settings()
