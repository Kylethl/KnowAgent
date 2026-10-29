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
    # 单文件上传大小上限（字节）：默认 20MB，与前端 documentUpload.ts 的 MAX_FILE_SIZE_MB 一致。
    # ⚠️ 该上限**只管文档类**（word / markdown）；图片/音频/视频的体积量级不同，
    #    分别由下方 P5 的 MAX_IMAGE_SIZE / MAX_AUDIO_SIZE / MAX_VIDEO_SIZE 控制。
    MAX_UPLOAD_SIZE: int = 20 * 1024 * 1024
    # 允许上传的扩展名白名单（逗号分隔，小写）。
    # P1 仅 Word / Markdown；P5 扩展图片 / 音频 / 视频（见 services/media_types.py 的分类映射）。
    # 注意：扩展名只是「第一道门」，真正的类型以文件头魔数校验为准。
    ALLOWED_EXTENSIONS: str = (
        "docx,doc,md,"
        "jpg,jpeg,png,webp,bmp,gif,"
        "mp3,wav,m4a,aac,flac,ogg,"
        "mp4,mov,m4v,avi,mkv,webm"
    )

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

    # ---------- RAG 检索增强（P4 功能1：/chat 检索增强） ----------
    # 总开关：关闭后 /chat 完全回到 P1/P2 的「无检索」行为（便于对比与排障）。
    RAG_ENABLED: bool = True
    # 最终注入 Prompt 的资料条数（重排后的 Top-K）：5 段对中文问答足够，
    # 再多会挤占上下文预算并抬高 token 成本。
    RAG_TOP_K: int = 5
    # 粗排候选数：先按向量距离多召回一些，再交由重排精挑，
    # 避免「向量近但关键词不相关」的切片占掉 Top-K 名额。
    RAG_CANDIDATE_K: int = 20
    # 余弦相似度下限（0~1，越大越严格）：低于该值的切片视为不相关，直接丢弃。
    # 0.40 由 `scripts/calibrate_rag_threshold.py` 实测标定（当前向量模型）：
    #   - 无关切片（跨文档两两比对）噪声 p95 ≈ 0.48，取过低的阈值会把无关资料放进 Prompt；
    #   - 真实相关问句的 Top-1 相似度落在 0.54~0.77，阈值过高会误杀真实问题。
    # 0.40 位于两者之间且偏向「宁可召回」——漏召回（答「未找到」）比多召回一段
    # 无关资料更难被用户接受，因为前者会让用户误以为知识库没有该内容。
    # ⚠️ 换 embedding 模型后必须用该脚本重新标定（不同模型相似度尺度不同）。
    RAG_SIMILARITY_THRESHOLD: float = 0.40
    # 注入 Prompt 的参考资料总字符上限（防止上下文无限膨胀，与 CHUNK_SIZE 联动估算）。
    RAG_MAX_CONTEXT_CHARS: int = 3000
    # 是否启用重排（rerank）。本项目未接专用 rerank 模型，
    # 采用「向量相似度 + 查询词覆盖率」的轻量启发式重排（见 services/retriever.py），
    # 无额外网络开销与费用；接入专用 rerank 服务后可在此处替换实现。
    RAG_RERANK_ENABLED: bool = True
    # 是否允许在 embedding 为 mock（未配置真实 QWEN_API_KEY）时仍执行检索。
    # 默认 False：mock 向量不具备语义相似度，召回结果等同于随机，
    # 强行检索会把无关资料塞进 Prompt 反而更差，故此时直接标记「检索未启用」。
    # 仅在做管线联调时可临时置 True。
    RAG_ALLOW_MOCK_EMBEDDING: bool = False
    # 检索结果缓存（P4-DB1「高频命中缓存到 Redis」）总开关。
    RAG_CACHE_ENABLED: bool = True
    # 缓存有效期（秒）：同一问题短期内重复提问直接复用召回结果，省一次 embedding 调用；
    # 知识库更新后最多等待该时长即可看到新内容，兼顾新鲜度。
    RAG_CACHE_TTL: int = 300
    # 缓存 key 前缀（带版本号，便于检索策略变更后整体失效旧缓存）。
    RAG_CACHE_KEY_PREFIX: str = "rag:retrieval:v1"
    # Redis 不可达时进程内回退缓存的最大条目数（LRU 淘汰）。
    RAG_CACHE_MAX_ITEMS: int = 256
    # 检索缓存专用的 Redis 地址：与 Celery 的 db0/db1 分开用 db2，避免互相干扰。
    REDIS_CACHE_URL: str = "redis://localhost:6379/2"

    # ---------- 入库状态自愈（僵死任务判定） ----------
    # 背景：入库任务被 kill / 进程重启时，documents.status 会永久停留在 'indexing'。
    # 此时前端（曾）把「重新入库」按钮一律置灰，后端也拒绝重跑，文档就再也入库不了。
    # 这里把「停留在 indexing 多久算僵死」定义为配置，超过即视为上次任务已中断，
    # 允许重新入库（前端据 AdminDocumentOut.indexing_stale 把按钮放开）。
    INDEXING_STALE_MINUTES: int = 10
    # 媒体类（图片/音视频）阈值放宽：OCR/ASR 走外部大模型，长视频转写可能远超 10 分钟，
    # 阈值过短会把「正在跑」误判成「已僵死」，导致并发重跑、重复计费。
    MEDIA_INDEXING_STALE_MINUTES: int = 60

    # ---------- 多模态提交（P5 功能1：图片 / 音频 / 视频） ----------
    # 各类型单文件体积上限（字节）。分级的原因：
    #   图片通常几百 KB~几 MB，给 10MB 足够容纳手机原图；
    #   音频（会议录音/播客）常在几十 MB，给 50MB；
    #   视频体积最大，给 200MB —— 再大就不是「知识库素材」而是长视频，应走专门的上传通道。
    # 说明：上限越大，单次请求的内存/磁盘与解析耗时越高（解析阶段要转写/OCR）。
    MAX_IMAGE_SIZE: int = 10 * 1024 * 1024
    MAX_AUDIO_SIZE: int = 50 * 1024 * 1024
    MAX_VIDEO_SIZE: int = 200 * 1024 * 1024
    # 是否校验「文件头魔数」与扩展名一致（默认开启）。
    # 仅靠扩展名可被轻易伪造（把 .exe 改名成 .png），魔数校验能挡住绝大多数伪装文件；
    # 极端场景（如未知容器格式）无法判定时按放行处理，避免误杀合法素材。
    MEDIA_VERIFY_MAGIC: bool = True
    # 病毒扫描（《需求清单》P5「病毒扫描（建议）」）：接 ClamAV 的 clamd TCP 接口。
    # 默认关闭；**一旦开启即 fail-closed**——clamd 不可达时拒收文件，
    # 因为「以为扫了其实没扫」比明确报错更危险。
    VIRUS_SCAN_ENABLED: bool = False
    VIRUS_SCAN_HOST: str = "127.0.0.1"
    VIRUS_SCAN_PORT: int = 3310
    VIRUS_SCAN_TIMEOUT: float = 10.0

    # ---------- 多模态解析（P5 功能2：OCR / ASR） ----------
    # 总开关：关闭后图片/音视频仍可提交与审核，但入库会明确失败（不静默跳过），
    # 避免「文档显示已入库却检索不到」的困惑。
    MEDIA_EXTRACT_ENABLED: bool = True
    # 多模态理解服务的地址与 Key。
    # 本项目复用千问百炼 compatible-mode 网关（与 embedding 同一个网关、同一个 Key），
    # 留空则自动回退到 QWEN_EMBEDDING_BASE_URL / QWEN_API_KEY，减少重复配置。
    MEDIA_LLM_BASE_URL: str = ""
    MEDIA_LLM_API_KEY: str = ""
    # 图片 OCR 模型：实测 qwen3.5-ocr 能按行精确提取中文文字（qwen-vl-ocr 只回坐标框，
    # 无法直接得到文本，故不采用）。换模型需保证「输入 image_url → 输出纯文本」。
    MEDIA_OCR_MODEL: str = "qwen3.5-ocr"
    # 音频/视频转写模型：实测 qwen3.8-omni-flash 支持 OpenAI 风格 input_audio（data URI），
    # 按「逐字转写」提示词可得到干净文本。网关上的 fun-asr / qwen-audio-asr 走的是
    # 实时 WebSocket 协议，compatible-mode 的 chat/completions 不接受，故不采用。
    MEDIA_ASR_MODEL: str = "qwen3.8-omni-flash"
    # 单次多模态请求超时（秒）：OCR 通常数秒，长音频转写可能数十秒，取较宽松值。
    MEDIA_EXTRACT_TIMEOUT: float = 180.0
    # 图片送模型前的最长边（像素）与 JPEG 压缩质量：
    # 手机原图 4000px 以上，base64 后体积巨大且 OCR 并不需要那么高分辨率，
    # 统一压到 1600px / q85 既能保证文字清晰，又能把请求体压到 1MB 内。
    MEDIA_IMAGE_MAX_EDGE: int = 1600
    MEDIA_IMAGE_JPEG_QUALITY: int = 85
    # 音频分片转写：单段秒数与最大段数。
    # 一次性把 1 小时音频塞给模型不现实（超时/超限），故用 ffmpeg 按时长切片、逐段转写后拼接。
    MEDIA_ASR_SEGMENT_SECONDS: int = 300
    MEDIA_ASR_MAX_SEGMENTS: int = 40
    # 视频是否额外抽帧做 OCR（默认关闭）：抽帧 OCR 对「PPT 类视频」有价值，
    # 但每帧都是一次模型调用，费用与时延成倍增长，需要时再显式打开。
    MEDIA_VIDEO_OCR_ENABLED: bool = False
    # 视频抽帧间隔（秒），仅当 MEDIA_VIDEO_OCR_ENABLED=true 时生效
    MEDIA_VIDEO_OCR_INTERVAL: int = 60
    # 从视频中提取音轨的工具：优先用配置的 ffmpeg；未安装时自动回退到
    # macOS 自带的 avconvert（仅供本地开发），两者都没有则明确报错。
    MEDIA_FFMPEG_BIN: str = "ffmpeg"

    # ---------- 异步任务（P3-B2：Celery + Redis） ----------
    # Celery 的消息队列与结果后端（P3-Dep1 引入 Redis）。
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    # 是否使用 Celery 执行入库任务。置 False 或未装 celery / Redis 不可达时，
    # 后端自动回退为「进程内后台任务」，保证开发期无 Redis 也能跑通入库闭环。
    USE_CELERY: bool = True
    # 多模态（OCR/ASR）任务专用队列名（P5-Dep1「worker 独立/扩容」）。
    # 为什么单独排队：音视频转写耗时可达数分钟且吃算力，和普通文档入库抢同一个 worker
    # 会让「刚上传的 Word」排在长视频后面；独立队列后可单独扩容媒体 worker。
    # 置空字符串表示不区分队列（全部走默认队列），便于小规模部署。
    CELERY_MEDIA_QUEUE: str = "media"

    # =================================================================
    # P7 生产加固 — 功能1：监控 / 日志 / 备份
    # -----------------------------------------------------------------
    # 全部开关集中在此，便于按环境（开发 / 预发 / 生产 / 私有化）差异化配置，
    # 业务代码只读配置、不写死任何阈值。
    # =================================================================

    # ---------- 结构化日志（P7-B1） ----------
    # 日志级别：DEBUG / INFO / WARNING / ERROR
    LOG_LEVEL: str = "INFO"
    # 日志格式：text（人类可读，本地开发）/ json（单行 JSON，生产采集用）
    LOG_FORMAT: str = "text"
    # 日志文件路径：留空表示只输出到 stdout（容器内推荐，交给 docker/fluentd 收集）。
    # 非空时额外写文件并按 LOG_MAX_BYTES 轮转（非容器部署时可落盘备查）。
    LOG_FILE: str = ""
    # 单个日志文件大小上限（字节）与保留的历史文件数
    LOG_MAX_BYTES: int = 20 * 1024 * 1024
    LOG_BACKUP_COUNT: int = 10
    # 是否输出访问日志（每个 HTTP 请求一行：方法/路径/状态/耗时/request_id）。
    # 关闭后仍保留错误日志，仅减少噪声（高频健康检查场景可关）。
    ACCESS_LOG_ENABLED: bool = True
    # 访问日志中是否包含 query string。默认 False：
    #   SSE 的 /chat 用 ?token= 传令牌，把 query 打进日志等于把令牌写进日志文件。
    ACCESS_LOG_INCLUDE_QUERY: bool = False

    # ---------- 错误上报 Sentry（P7-B1） ----------
    # DSN 留空即完全关闭（不引入任何网络上报，符合「数据不出域」默认姿态）。
    SENTRY_DSN: str = ""
    # 上报环境标识：留空则回退到 APP_ENV（development / production）
    SENTRY_ENVIRONMENT: str = ""
    # 性能追踪采样率（0~1）：0 表示只上报错误、不做 tracing，成本最低
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    # 是否上报请求中的个人信息（IP、Cookie、请求头）。
    # 合规默认 False：私有化/等保场景下 PII 不应离开本地。
    SENTRY_SEND_DEFAULT_PII: bool = False

    # ---------- LLM 调用限流 / 配额（P7-B1） ----------
    # 总开关：关闭后不做任何限流（压测或单机私有化可按需关闭）。
    LLM_RATE_LIMIT_ENABLED: bool = True
    # 单用户每分钟最多发起多少次对话（滑动窗口按自然分钟计数）。
    # 取值参考：正常用户每分钟提问 1~5 次，20 次足以容纳连点与重试，又能挡住脚本刷。
    LLM_RATE_LIMIT_PER_MINUTE: int = 20
    # 单用户每日最多发起多少次对话（自然日，按服务器时区）。
    LLM_RATE_LIMIT_PER_DAY: int = 300
    # 单用户每日 token 配额（输入+输出合计）；0 表示不限制。
    # 为什么要 token 配额：次数配额挡不住「一次塞 8000 字 + 长回答」的成本放大。
    LLM_DAILY_TOKEN_QUOTA: int = 200_000
    # 限流计数在 Redis 中的 key 前缀（带版本号便于策略变更后整体失效）
    LLM_RATE_LIMIT_KEY_PREFIX: str = "ratelimit:llm:v1"
    # 限流专用 Redis 地址：留空则复用 REDIS_CACHE_URL（db2），避免多一处配置。
    RATE_LIMIT_REDIS_URL: str = ""
    # Redis 不可达时的策略：True=放行（fail-open，保业务可用）；
    # False=拒绝（fail-closed，保成本可控）。默认放行并告警——
    # 限流是保护措施，不应在依赖故障时把整个问答功能打死。
    RATE_LIMIT_FAIL_OPEN: bool = True

    # ---------- 监控指标（P7-Dep1：Prometheus） ----------
    # 总开关：关闭后 /metrics 返回 404，且不注册中间件指标（省一点开销）。
    METRICS_ENABLED: bool = True
    # 指标暴露路径（nginx 侧建议只对内网/白名单开放）
    METRICS_PATH: str = "/metrics"
    # 抓取令牌：非空时访问 /metrics 必须携带 Bearer（或 ?token=）。
    # 为什么需要：/metrics 会暴露接口拓扑与流量规模，生产不宜完全公开。
    METRICS_TOKEN: str = ""
    # 慢请求阈值（毫秒）：超过即在访问日志中标记 slow=true，便于 grep 定位
    METRICS_SLOW_REQUEST_MS: int = 2000
    # Grafana 面板地址（仅用于前端状态页展示「打开监控面板」链接，可留空）
    GRAFANA_URL: str = ""

    # ---------- 备份（P7-DB1 / P7-Dep1） ----------
    # 总开关：关闭后定时任务不注册，手动备份接口返回「未启用」
    BACKUP_ENABLED: bool = True
    # 备份文件目录（相对 backend 目录；容器内建议挂载独立卷/对象存储）
    BACKUP_DIR: str = "data/backups"
    # pg_dump 可执行文件：容器内为 pg_dump（随 postgres 客户端安装），
    # 本机可用 Homebrew 的绝对路径（如 /opt/homebrew/opt/postgresql@16/bin/pg_dump）
    BACKUP_PG_DUMP_BIN: str = "pg_dump"
    # 单次备份的超时（秒）：库越大越久，超时后杀掉子进程并标记失败
    BACKUP_TIMEOUT: float = 1800.0
    # 保留策略：按天数与个数双重约束，任一超限即清理（0 表示不按该维度清理）
    BACKUP_RETENTION_DAYS: int = 14
    BACKUP_RETENTION_COUNT: int = 30
    # 每日备份执行时刻（小时，0~23，服务器本地时区），配合 Celery beat 使用
    BACKUP_SCHEDULE_HOUR: int = 3
    # 备份完成后是否顺带做一次 Redis BGSAVE 并校验持久化状态
    BACKUP_REDIS_CHECK: bool = True

    # =================================================================
    # P7 生产加固 — 功能2：合规与私有化部署
    # =================================================================

    # ---------- 数据不出域（P7-B2） ----------
    # 模式：
    #   standard — 仅记录出站去向，不做拦截（普通公有云部署）
    #   strict   — 强制校验出站域名白名单，未在白名单内的外部调用直接失败
    #              （等保/金融/政务私有化场景）
    DATA_RESIDENCY_MODE: str = "standard"
    # 允许出站的域名白名单（逗号分隔，支持后缀匹配：.aliyuncs.com 匹配其子域）。
    # strict 模式下留空表示「只允许内网/本机地址」（即完全本地化模型部署）。
    OUTBOUND_ALLOWED_HOSTS: str = ""
    # 数据驻留区域标识（仅用于合规展示与审计记录，如 cn / cn-shanghai / on-prem）
    DATA_REGION: str = "cn"

    # ---------- 审计日志（P7-B2） ----------
    # 总开关：关闭后不再写审计（仅保留普通日志），一般只在压测时关闭。
    AUDIT_ENABLED: bool = True
    # 审计日志保留天数：超期由定时任务清理（等保通常要求 ≥180 天）
    AUDIT_RETENTION_DAYS: int = 180
    # 是否审计「读操作」（对话、列表查询等）。
    # 默认 False：这些操作量大且价值低，审计它们会把库写满、并淹没真正的敏感动作。
    AUDIT_LOG_READS: bool = False

    # ---------- 日志与审计的派生属性 ----------

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

    @property
    def is_rag_ready(self) -> bool:
        """判断当前配置下 RAG 检索是否「有意义」可跑（P4 功能1）。

        两个条件同时满足才视为可用：
          1. RAG_ENABLED 已开启；
          2. embedding 具备语义能力——要么已配置真实 QWEN Key，
             要么显式允许 mock（RAG_ALLOW_MOCK_EMBEDDING=True，仅供管线联调）。

        说明：mock 向量由文本哈希生成，相似度不反映语义，
        用它检索等于随机挑资料，因此默认不启用检索（返回「未启用」而非乱答）。
        """
        if not self.RAG_ENABLED:
            return False
        return self.is_qwen_configured or self.RAG_ALLOW_MOCK_EMBEDDING

    # ---------- 多模态相关派生配置（P5） ----------

    @property
    def media_llm_base_url(self) -> str:
        """多模态理解服务地址：未单独配置时复用 embedding 网关（同一个百炼网关）。

        Returns:
            形如 https://.../compatible-mode/v1 的**不含** /chat/completions 的基础地址。
        """
        return (self.MEDIA_LLM_BASE_URL or "").strip() or self.QWEN_EMBEDDING_BASE_URL

    @property
    def media_llm_api_key(self) -> str:
        """多模态理解服务的 Key：未单独配置时复用 QWEN_API_KEY。"""
        return (self.MEDIA_LLM_API_KEY or "").strip() or self.QWEN_API_KEY

    @property
    def is_media_ocr_ready(self) -> bool:
        """判断图片 OCR 是否可用（P5 功能2 启动自检与前端提示用）。

        与 embedding 不同，OCR **没有 mock 兜底**：伪文字一旦入库会污染知识库，
        因此未配置 Key 时直接判定为不可用，入库会以明确错误失败。
        """
        if not self.MEDIA_EXTRACT_ENABLED:
            return False
        key = (self.media_llm_api_key or "").strip()
        return bool(key) and not key.startswith("sk-REPLACE_ME")

    @property
    def is_media_asr_ready(self) -> bool:
        """判断音频/视频转写是否可用（同 OCR，没有 mock 兜底）。"""
        return self.is_media_ocr_ready

    def max_upload_size_for(self, file_type: str) -> int:
        """按文件类型返回单文件体积上限（字节）。

        Args:
            file_type: 文档类型（word / markdown / image / audio / video）。

        Returns:
            对应类型的上限字节数；未知类型回退到文档上限 MAX_UPLOAD_SIZE。
        """
        if file_type == "image":
            return self.MAX_IMAGE_SIZE
        if file_type == "audio":
            return self.MAX_AUDIO_SIZE
        if file_type == "video":
            return self.MAX_VIDEO_SIZE
        return self.MAX_UPLOAD_SIZE

    def max_upload_size_mb_for(self, file_type: str) -> float:
        """按类型返回上限的 MB 表示（用于错误提示与前端文案）。"""
        return round(self.max_upload_size_for(file_type) / (1024 * 1024), 2)

    # ---------- P7 派生配置 ----------

    @property
    def is_json_log(self) -> bool:
        """是否使用 JSON 结构化日志（生产采集 ELK / Loki 时为 True）。"""
        return (self.LOG_FORMAT or "").strip().lower() == "json"

    @property
    def sentry_environment(self) -> str:
        """Sentry 环境标识：未单独配置时回退到 APP_ENV。"""
        return (self.SENTRY_ENVIRONMENT or "").strip() or self.APP_ENV

    @property
    def is_sentry_enabled(self) -> bool:
        """是否启用 Sentry 上报（DSN 非空才算启用）。

        说明：DSN 为空时不引入任何外发流量——私有化部署默认即如此，
        与「数据不出域」的默认姿态一致（合规审计项会检查这一点）。
        """
        return bool((self.SENTRY_DSN or "").strip())

    @property
    def rate_limit_redis_url(self) -> str:
        """限流专用 Redis 地址：未单独配置时复用检索缓存所用的 db2。"""
        return (self.RATE_LIMIT_REDIS_URL or "").strip() or self.REDIS_CACHE_URL

    @property
    def outbound_allowed_hosts_list(self) -> List[str]:
        """把逗号分隔的出站白名单解析为小写列表（去空）。"""
        return [
            item.strip().lower()
            for item in self.OUTBOUND_ALLOWED_HOSTS.split(",")
            if item.strip()
        ]

    @property
    def is_strict_residency(self) -> bool:
        """是否为「严格数据不出域」模式（strict 才做拦截）。"""
        return (self.DATA_RESIDENCY_MODE or "").strip().lower() == "strict"

    @property
    def backup_dir_abs(self) -> str:
        """备份目录的绝对路径（相对路径以 backend 目录为基准）。

        为什么不直接用 cwd：uvicorn 可能从任意目录启动，
        用文件位置推导 backend 根目录可保证「无论在哪启动都落在同一处」。
        """
        import os

        if os.path.isabs(self.BACKUP_DIR):
            return self.BACKUP_DIR
        # __file__ = backend/app/config.py → dirname(dirname()) = backend
        backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(backend_root, self.BACKUP_DIR)

    @property
    def log_file_abs(self) -> str:
        """日志文件的绝对路径（留空表示只输出 stdout）。"""
        import os

        if not (self.LOG_FILE or "").strip():
            return ""
        if os.path.isabs(self.LOG_FILE):
            return self.LOG_FILE
        backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(backend_root, self.LOG_FILE)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局唯一的配置实例。

    使用 lru_cache 做进程内单例，避免每次请求重复解析 .env。
    单测中需要覆盖配置时，可先执行 get_settings.cache_clear()。
    """
    return Settings()
