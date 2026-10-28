# 接口的请求 / 响应数据模型（P1-B1）
# 职责：用 Pydantic 明确 /chat 等接口的入参约束，FastAPI 据此自动校验与生成 OpenAPI 文档。

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# 消息角色：与 DeepSeek（OpenAI 兼容协议）一致
Role = Literal["system", "user", "assistant"]


# =====================================================================
# 文档上传（P1-B2 / P1-F3 契约）
# 字段命名严格对齐前端 src/types.ts 的 DocumentRecord，便于前端直接反序列化。
# =====================================================================

class DocumentUploadResponse(BaseModel):
    """POST /documents/upload 的成功响应。

    字段与前端 DocumentRecord 一一对应：
      id / filename / type / status / size / storage_path / created_at
    其中后端库内字段 original_path → storage_path，size_bytes → size。
    """

    id: str = Field(..., description="文档唯一 ID（后端 UUID）")
    filename: str = Field(..., description="原始文件名（展示用）")
    # 文档类型：word（.doc/.docx）/ markdown（.md）/ image / audio / video（P5）
    type: str = Field(..., description="文档类型：word / markdown / image / audio / video")
    # 生命周期状态：P1 上传成功固定为 pending（待审核）
    status: str = Field(..., description="文档状态：pending / indexed / rejected")
    # 文件字节数（对应库内 size_bytes）
    size: int = Field(..., description="文件大小（字节）")
    # 对象存储相对路径（不含 bucket），仅展示用，禁止前端拼接成下载链接
    storage_path: str = Field(..., description="对象存储相对路径（展示用）")
    # 提交时间（ISO 8601）
    created_at: str = Field(..., description="提交时间（ISO 8601 字符串）")


class ChatMessage(BaseModel):
    """一条对话消息（用于历史上下文注入）。"""

    role: Role = Field(..., description="消息角色：system / user / assistant")
    content: str = Field(..., description="消息正文")


class ChatRequest(BaseModel):
    """POST /chat 的请求体。

    P1 阶段前端使用 EventSource（仅支持 GET），因此实际走 GET 分支；
    POST 分支为长文本提问与后续阶段（P2 会话历史）预留。
    """

    # 用户本轮提问，必填且不能为空
    message: str = Field(..., min_length=1, max_length=8000, description="用户提问内容")
    # 会话 ID：不传则由后端按「单会话」处理（P2 起由前端传入真实会话）
    conversation_id: Optional[str] = Field(default=None, description="会话 ID，用于多轮上下文")
    # 可选：由客户端显式携带的历史消息（不传则用后端会话记忆）
    history: Optional[List[ChatMessage]] = Field(default=None, description="客户端携带的历史消息")


class HealthResponse(BaseModel):
    """健康检查响应（部署冒烟测试用）。"""

    status: str = Field(..., description="服务状态：ok / degraded")
    app: str = Field(..., description="应用名称")
    env: str = Field(..., description="运行环境")
    # 大模型是否配置了可用 Key，便于排查「能访问但答不了」的问题
    llm_ready: bool = Field(..., description="DeepSeek 是否已配置可用 API Key")
    # ---- P5：多模态解析能力（部署自检用） ----
    media_ocr_ready: bool = Field(False, description="图片 OCR 是否可用（模型 Key 已配置）")
    media_asr_ready: bool = Field(False, description="音视频 ASR 是否可用（模型 Key 已配置）")
    media_ffmpeg: bool = Field(False, description="是否找到可用的 ffmpeg（视频提音轨/音频切片）")


# =====================================================================
# 会话与历史（P2-B1 / P2-F1 契约）
# 字段命名对齐前端 src/types.ts 的 Conversation / ConversationMessage，
# 便于前端直接反序列化。时间统一以 ISO 8601 字符串返回。
# =====================================================================

class ConversationCreate(BaseModel):
    """POST /conversations 的请求体：新建会话时可选携带标题。

    不传 title 时由后端在首条用户消息到达时自动回填（见 conversation_service.append_turn）。
    """

    title: Optional[str] = Field(default=None, description="会话标题（可选，留空则首条问题自动生成）")


class ConversationOut(BaseModel):
    """单个会话的对外视图（列表项与详情共用基础字段）。"""

    id: str = Field(..., description="会话唯一 ID（后端 UUID）")
    title: Optional[str] = Field(default=None, description="会话标题（首条问题自动生成）")
    model: str = Field(..., description="对话使用的模型（DeepSeek-Flash）")
    created_at: str = Field(..., description="创建时间（ISO 8601）")
    updated_at: str = Field(..., description="更新时间（ISO 8601，最近活跃排序用）")


class ConversationSummary(ConversationOut):
    """会话列表项：在基础字段上附加消息条数，便于前端展示预览。"""

    message_count: int = Field(..., description="该会话下的消息条数")


class CitationOut(BaseModel):
    """单条答案引用来源（P4-B2 / P4-F2 契约）。

    契约来源：《需求清单》P4 功能2
      - 后端：`/chat` 返回 cite 列表（doc_id + chunk 片段）
      - 前端：回答下方展示引用文档卡片（标题、片段、可跳转）
      - 数据存储：引用关系存 messages 附加字段

    字段与 services/retriever.build_citations() 的输出严格一致，
    该字典同时用于 SSE citations 帧与 messages.citations 持久化，
    保证「实时下发的」与「历史回看的」结构完全相同，前端只需一套渲染逻辑。
    """

    # 资料编号：与 Prompt 中 [1][2] 标注、回答正文里的编号一一对应
    index: int = Field(..., description="资料编号（对应回答中的 [n] 标注）")
    doc_id: str = Field(..., description="来源文档 ID（跳转原文用）")
    doc_title: str = Field(..., description="来源文档标题（原文件名）")
    file_type: str = Field("", description="文档类型：word / markdown")
    # 切片 ID 与序号：跳转定位到具体片段
    chunk_id: str = Field(..., description="切片 ID（跳转原文片段用）")
    chunk_index: int = Field(0, description="切片在文档内的序号（从 0 开始）")
    heading: str = Field("", description="所属章节标题（可为空）")
    snippet: str = Field(..., description="片段摘要（已截断，卡片展示用）")
    # 相关度：score 为综合排序分；vector_score 为语义相似度（展示更直观）
    score: float = Field(0.0, description="综合排序分")
    vector_score: float = Field(0.0, description="语义相似度（0~1）")
    # 是否被回答真正引用：False 表示「作为参考资料注入但模型未标注引用」
    cited: bool = Field(False, description="是否被回答直接引用")


class ConversationMessageOut(BaseModel):
    """历史消息项：对应 messages 表的一行。"""

    id: str = Field(..., description="消息唯一 ID")
    role: str = Field(..., description="角色：user / assistant / system")
    content: str = Field(..., description="消息正文")
    created_at: str = Field(..., description="发送时间（ISO 8601）")
    # P4-B2：助手消息的引用来源（user 消息与「当时未启用检索」的旧消息为空）
    citations: List[CitationOut] = Field(
        default_factory=list, description="该条回答的引用来源（P4 功能2）"
    )
    # P6-F2：当前登录用户对这条回答的反馈（未评价 / 非助手消息为 None）。
    # 必须随历史一起下发：否则刷新页面后点赞/点踩按钮会「忘记」已评价过，
    # 用户会重复提交，且看不到自己当时填的差评原因。
    feedback: Optional["FeedbackOut"] = Field(
        default=None, description="当前用户对该回答的反馈（P6 功能2）"
    )


class ConversationDetail(ConversationOut):
    """会话详情：基础字段 + 按时间正序的历史消息列表。"""

    messages: List[ConversationMessageOut] = Field(
        default_factory=list, description="历史消息（created_at 正序）"
    )


# =====================================================================
# 用户鉴权（P2-B2 / P2-F2 契约）
# 字段命名对齐前端 src/types.ts 的 AuthUser / LoginResult，便于直接反序列化。
# 安全约定：任何响应都**不返回** password_hash 等敏感字段。
# =====================================================================

class UserOut(BaseModel):
    """用户对外视图（脱敏）：绝不包含 password_hash。"""

    id: str = Field(..., description="用户唯一 ID（后端 UUID）")
    username: str = Field(..., description="登录名")
    email: Optional[str] = Field(default=None, description="邮箱（可为空）")
    # 角色：user / admin。P2 预留，P3 RBAC 生效
    role: str = Field(..., description="角色：user / admin")
    created_at: str = Field(..., description="注册时间（ISO 8601）")


class RegisterRequest(BaseModel):
    """POST /auth/register 请求体。

    说明：不接收 role —— 角色一律由服务端决定为 'user'，
    防止客户端自行提权为 admin（注册接口绝不允许指定角色）。
    """

    # 登录名：3~64 字符，字母/数字/下划线/中文等，由后端做唯一性校验
    username: str = Field(..., min_length=3, max_length=64, description="登录名（3~64 字符，唯一）")
    # 明文密码：仅在请求传输中出现，服务端 bcrypt 哈希后落库，绝不存明文
    password: str = Field(..., min_length=8, max_length=72, description="密码（8~72 位，需含字母和数字）")
    # 邮箱可选；填写时做唯一性校验
    email: Optional[str] = Field(default=None, description="邮箱（可选，唯一）")


class LoginRequest(BaseModel):
    """POST /auth/login 请求体。"""

    username: str = Field(..., description="登录名")
    password: str = Field(..., description="密码")


class TokenResponse(BaseModel):
    """登录/注册成功的令牌响应。

    前端据此把 access_token 存入 localStorage，
    并在后续请求的 Authorization: Bearer <token> 头中携带。
    """

    access_token: str = Field(..., description="JWT 访问令牌")
    # 固定为 bearer，符合 RFC 6750
    token_type: str = Field(default="bearer", description="令牌类型，固定 bearer")
    # 有效期（秒）：便于前端预估过期时间，提前引导重新登录
    expires_in: int = Field(..., description="令牌有效期（秒）")
    user: UserOut = Field(..., description="当前登录用户（脱敏）")


# =====================================================================
# 管理员审核（P3-B1 / P3-F1 契约）
# ---------------------------------------------------------------------
# 契约来源：《需求清单》P3 功能1「管理员审核」
#   - 前端：管理员后台页、待审核列表、文档预览、通过/驳回（驳回必填理由）
#   - 后端：/admin/documents 列表、/admin/documents/{id}/review 更新状态
# 字段命名与前端 AdminDocument 对齐；绝不返回 original_path 之外的存储细节，
# 且**不提供**任何可直接拼接的下载直链（下载走后端接口，便于鉴权与审计）。
# =====================================================================

class AdminDocumentOut(BaseModel):
    """管理员视角的文档条目（审核队列列表项）。"""

    id: str = Field(..., description="文档唯一 ID")
    filename: str = Field(..., description="原始文件名")
    file_type: str = Field(..., description="文档类型：word / markdown")
    # 生命周期状态：pending / approved / rejected / indexing / indexed
    status: str = Field(..., description="生命周期状态")
    # 审核结论（P3-DB1 新增列）：pending / approved / rejected
    review_status: str = Field(..., description="审核结论：pending / approved / rejected")
    size_bytes: int = Field(0, description="文件字节数")
    # 提交人用户名（脱敏展示；提交人已注销时为 None）
    uploader: Optional[str] = Field(default=None, description="提交人用户名（可能为空）")
    # 审核人用户名（未审核时为 None）
    reviewer: Optional[str] = Field(default=None, description="审核人用户名（未审核为空）")
    review_note: Optional[str] = Field(default=None, description="审核备注（驳回理由）")
    reviewed_at: Optional[str] = Field(default=None, description="审核时间（ISO 8601）")
    created_at: str = Field(..., description="提交时间（ISO 8601）")
    # ---- P3-B2 入库状态字段（供管理员判断入库结果） ----
    # 已写入 document_chunks 的切片数；未入库或已下线为 0
    chunk_count: int = Field(0, description="已入库切片数（0 表示未入库/已下线）")
    # 最近一次成功入库的时间；未入库为 None
    indexed_at: Optional[str] = Field(default=None, description="入库完成时间（ISO 8601）")
    # 最近一次入库失败的原因；成功入库后为空。前端据此展示「入库失败」
    last_index_error: Optional[str] = Field(
        default=None, description="最近一次入库失败原因（成功为空）"
    )
    # ---- 入库状态自愈（P5 修复：僵死任务可重新入库） ----
    # True 表示 status='indexing' 但已超过阈值仍未完成——上次入库被中断（进程重启 /
    # worker 被杀），任务不会再自行结束。前端据此**放开**「重新入库」按钮，
    # 否则该文档会永久卡在入库中，管理员只剩「预览」可用。
    indexing_stale: bool = Field(
        default=False, description="是否停留在入库中且已超时（可重新入库）"
    )
    # 停留在 indexing 的已持续秒数（非 indexing 为 None），用于提示「已卡住 N 分钟」
    indexing_elapsed_seconds: Optional[int] = Field(
        default=None, description="入库中已持续秒数（非入库中为空）"
    )
    # ---- P4：向量时效性（防「mock 向量 / 旧模型向量」混入检索） ----
    # 入库时使用的向量模型签名：模型名，或未配置 Key 时的 "mock"；历史数据为 None
    embedding_model: Optional[str] = Field(
        default=None, description="入库时使用的向量模型签名（mock 或模型名）"
    )
    # True 表示向量与当前配置不可比（mock/换模型/历史未记录），建议重新入库
    embedding_stale: bool = Field(
        default=False, description="向量是否已过期（建议重新入库）"
    )
    # ---- P5：多模态解析元信息（图片/音视频入库后才有值） ----
    # 形如 {kind, provider, model, chars, duration_seconds, segments, extractor}
    extract_meta: Optional[dict] = Field(
        default=None, description="OCR/ASR 解析元信息（文档类为空）"
    )


class AdminDocumentListResponse(BaseModel):
    """GET /admin/documents 的响应：当前页列表 + 总数（供前端分页）。"""

    items: List[AdminDocumentOut] = Field(
        default_factory=list, description="当前页文档列表"
    )
    total: int = Field(..., description="满足条件的总条数")
    limit: int = Field(..., description="每页条数")
    offset: int = Field(..., description="当前偏移量")


class ReviewRequest(BaseModel):
    """POST /admin/documents/{id}/review 的请求体。

    安全约定：不接受 status / review_status 字段——审核结论只能由 action 决定，
    防止管理员误传或直接把文档改成任意状态。
    """

    # 审核动作：approved（通过）/ rejected（驳回）
    action: str = Field(..., description="审核动作：approved / rejected")
    # 驳回理由：action=rejected 时必填（服务端二次校验，不只靠前端校验）
    note: Optional[str] = Field(default=None, description="审核备注；驳回时必填理由")


class ReviewResult(BaseModel):
    """审核动作的响应：回传更新后的文档条目，前端可直接就地更新表格行。"""

    document: AdminDocumentOut = Field(..., description="更新后的文档")


class DocumentPreviewResponse(BaseModel):
    """GET /admin/documents/{id}/preview 的响应：文本预览。"""

    id: str = Field(..., description="文档唯一 ID")
    filename: str = Field(..., description="原始文件名")
    file_type: str = Field(..., description="文档类型：word / markdown / image / audio / video")
    text: str = Field(..., description="预览文本（可能已截断）")
    truncated: bool = Field(..., description="是否因超长被截断")
    # 无法解析（如旧版 .doc）时给出说明，前端据此提示管理员下载查看
    previewable: bool = Field(..., description="是否成功提取出文本内容")
    # P5：多模态解析元信息（图片/音视频），前端据此展示「OCR/ASR 用了哪个模型、耗时多久」
    extract_meta: Optional[dict] = Field(
        default=None, description="OCR/ASR 解析元信息（文档类为空）"
    )


# =====================================================================
# 文档入库（P3-B2 契约）
# ---------------------------------------------------------------------
# 契约来源：《需求清单》P3 功能2「文档入库（RAG 管线）」
#   - 前端：审核通过后用户侧文档状态变更「已入库」
#   - 后端：Celery 异步任务解析 → 切片 → embedding → 写向量；支持重入库 / 下线
# =====================================================================

class IndexResult(BaseModel):
    """入库类操作（入库 / 重入库 / 下线）的统一响应。

    queued=True 表示任务已投递到 Celery 异步执行（前端应展示「入库中」）；
    queued=False 表示已在进程内同步执行完成（无 Redis 时的回退路径）。
    """

    document: AdminDocumentOut = Field(..., description="操作后的文档（含入库状态）")
    # 是否异步执行：True=Celery 异步；False=进程内同步（本地无 Redis 回退）
    queued: bool = Field(..., description="是否投递到 Celery 异步执行")
    message: str = Field(..., description="可读的结果说明（便于前端提示）")


class MyDocumentOut(BaseModel):
    """用户视角的文档条目（「我的文档」列表项，P3-F1 状态展示用）。

    与管理员视角的区别：只暴露本人提交文档的状态，不含审核人等管理信息。
    """

    id: str = Field(..., description="文档唯一 ID")
    filename: str = Field(..., description="原始文件名")
    file_type: str = Field(..., description="文档类型：word / markdown")
    # 生命周期状态：pending / approved / rejected / indexing / indexed
    status: str = Field(..., description="生命周期状态（indexed 即「已入库」）")
    # 审核结论：pending / approved / rejected
    review_status: str = Field(..., description="审核结论")
    size_bytes: int = Field(0, description="文件字节数")
    # 切片数：>0 表示已入库（前端据此展示「已入库」标签）
    chunk_count: int = Field(0, description="已入库切片数")
    # 驳回理由：提交人被驳回时展示，便于其修改后重新提交
    review_note: Optional[str] = Field(default=None, description="审核备注（驳回理由）")
    created_at: str = Field(..., description="提交时间（ISO 8601）")
    indexed_at: Optional[str] = Field(default=None, description="入库完成时间（ISO 8601）")
    # 最近一次入库失败原因；成功入库后为空。
    # 注意：入库失败会回退 status 为 'approved'，故需此字段区分「已通过待入库」与「入库失败」
    last_index_error: Optional[str] = Field(
        default=None, description="最近一次入库失败原因（成功为空）"
    )
    # ---- P4：向量时效性（用户侧同样需要知道「为什么检索不到我刚入库的文档」） ----
    # 入库时使用的向量模型签名：模型名，或未配置 Key 时的 "mock"；历史数据为 None
    embedding_model: Optional[str] = Field(
        default=None, description="入库时使用的向量模型签名（mock 或模型名）"
    )
    # True 表示向量已过期（mock 向量 / 换过模型 / 历史未记录），需重新入库才能参与检索
    embedding_stale: bool = Field(
        default=False, description="向量是否已过期（建议联系管理员重新入库）"
    )
    # ---- P5：多模态解析元信息（图片/音视频入库后才有值） ----
    extract_meta: Optional[dict] = Field(
        default=None, description="OCR/ASR 解析元信息（文档类为空）"
    )


class MyDocumentListResponse(BaseModel):
    """GET /documents 的响应：当前登录用户提交的文档列表。"""

    items: List[MyDocumentOut] = Field(
        default_factory=list, description="当前用户的文档列表（按提交时间倒序）"
    )
    total: int = Field(..., description="文档总条数")


# =====================================================================
# 引用原文片段（P4-F2「可跳转」的落地接口）
# ---------------------------------------------------------------------
# 为什么需要它：
#   《需求清单》要求引用卡片「可跳转」。知识库是共享的，但用户手上没有
#   原文文件（也不该为此开放文件下载），因此提供「按 chunk_id 取片段正文」的只读接口：
#   点击引用卡片即在此展开该段原文（含相邻片段导航），既满足跳转语义，
#   又不扩大数据暴露面（仅限已入库文档，且不提供原始文件）。
# =====================================================================


class ChunkDetailResponse(BaseModel):
    """GET /documents/{doc_id}/chunks/{chunk_id} 的响应。"""

    doc_id: str = Field(..., description="文档 ID")
    doc_title: str = Field(..., description="文档标题（原文件名）")
    file_type: str = Field("", description="文档类型：word / markdown")
    chunk_id: str = Field(..., description="切片 ID")
    chunk_index: int = Field(..., description="切片在文档内的序号（从 0 开始）")
    total_chunks: int = Field(..., description="该文档的切片总数（用于「第 n/N 段」展示）")
    heading: str = Field("", description="所属章节标题（可为空）")
    content: str = Field(..., description="切片完整正文（非摘要）")
    # 相邻切片 ID：前端据此提供「上一段 / 下一段」，让用户能看到上下文
    prev_chunk_id: Optional[str] = Field(default=None, description="上一段切片 ID")
    next_chunk_id: Optional[str] = Field(default=None, description="下一段切片 ID")


# =====================================================================
# P6 功能1：审核通知（站内信）
# ---------------------------------------------------------------------
# 契约来源：《需求清单》P6 功能1「审核通知」
#   - 前端：站内信 / Toast 提示「文档已通过 / 被驳回」
#   - 后端：状态变更触发通知；数据存储 notifications 表
# 字段命名与前端 NotificationItem 严格对齐，便于直接反序列化。
#
# 设计取舍：type 用字符串枚举（document_approved / document_rejected），
# 前端据此决定图标与配色；title/content 是审核时的快照，不随文档变更而改写。
# =====================================================================

# 通知类型：审核通过 / 审核驳回（与 notifications.type 的 CHECK 约束一致）
NotificationType = Literal["document_approved", "document_rejected"]


class NotificationOut(BaseModel):
    """单条站内信（列表项 / Toast 数据源）。"""

    id: str = Field(..., description="通知唯一 ID")
    type: str = Field(..., description="通知类型：document_approved / document_rejected")
    title: str = Field(..., description="标题（一句话结论）")
    content: str = Field(..., description="正文（含文件名与驳回理由等细节）")
    # 关联文档：前端可据此定位「我的文档」中的条目；文档被清理后为 None
    doc_id: Optional[str] = Field(default=None, description="关联文档 ID（可空）")
    # 已读时间：None 表示未读。前端据此渲染未读圆点
    read_at: Optional[str] = Field(default=None, description="已读时间（ISO 8601，空=未读）")
    created_at: str = Field(..., description="创建时间（ISO 8601）")


class NotificationListResponse(BaseModel):
    """GET /notifications 的响应：当前页 + 总数 + 未读数。"""

    items: List[NotificationOut] = Field(default_factory=list, description="当前页通知")
    total: int = Field(..., description="满足条件的总条数")
    unread_count: int = Field(..., description="当前用户的未读通知数（角标）")
    limit: int = Field(..., description="每页条数")
    offset: int = Field(..., description="当前偏移量")


class NotificationUnreadResponse(BaseModel):
    """GET /notifications/unread-count 的响应（轻量轮询接口）。

    单独提供的原因：前端每 30 秒轮询一次未读数，若每次都拉完整列表，
    会白白传输正文并抬高数据库压力，只回一个数字最省。
    """

    unread_count: int = Field(..., description="未读通知数")


class NotificationReadResponse(BaseModel):
    """标记已读的响应：回传最新未读数，前端无需再请求一次。"""

    notification: NotificationOut = Field(..., description="被标记的通知")
    unread_count: int = Field(..., description="标记后剩余的未读数")


class NotificationReadAllResponse(BaseModel):
    """全部标记已读的响应。"""

    updated: int = Field(..., description="本次标记为已读的条数")
    unread_count: int = Field(0, description="标记后的未读数（恒为 0）")


# =====================================================================
# P6 功能2：答案反馈与评估
# ---------------------------------------------------------------------
# 契约来源：《需求清单》P6 功能2「答案反馈与评估」
#   - 前端：回答点赞/点踩按钮 + 反馈表单
#   - 后端：/messages/{id}/feedback 存储反馈；反馈用于后续离线优化
#   - 数据存储：feedback 表
#
# 说明：评价对象是「一条助手回答」（messages.id），不是会话——
# 只有精确到单条回答，才能做「哪类问题答不好」的归因分析。
# =====================================================================

# 评价结论：点赞 / 点踩（与前端的两个按钮一一对应）
FeedbackRating = Literal["up", "down"]

# 点踩归因：答非所问 / 内容错误 / 资料不符 / 其他
# 这套分类直接对应后续优化动作：
#   off_topic   → 调整 prompt 的角色与回答范围
#   wrong       → 检查模型能力或补充知识库内容
#   unsupported → 检查检索召回与引用标注
#   other       → 人工查看详情
FeedbackReason = Literal["off_topic", "wrong", "unsupported", "other"]

# 反馈备注的最大长度：与 service 层校验保持一致，防止超长文本进库
FEEDBACK_COMMENT_MAX_LEN = 1000


class FeedbackRequest(BaseModel):
    """POST /messages/{message_id}/feedback 的请求体。

    安全约定：不接受 message_id / user_id —— 前者来自路径，后者由 JWT 解析，
    杜绝「替别人提交反馈」或「给不属于自己的消息刷评价」。
    """

    rating: FeedbackRating = Field(..., description="评价结论：up（赞）/ down（踩）")
    # 点踩归因：可选（后端不强制，避免为了提交评价而卡住用户），前端表单会引导填写
    reason: Optional[FeedbackReason] = Field(
        default=None, description="点踩归因：off_topic / wrong / unsupported / other"
    )
    # 补充说明：可选，自由文本；超过 FEEDBACK_COMMENT_MAX_LEN 由服务端拒绝
    comment: Optional[str] = Field(
        default=None, description=f"补充说明（可选，最多 {FEEDBACK_COMMENT_MAX_LEN} 字）"
    )


class FeedbackOut(BaseModel):
    """单条反馈的对外视图（提交结果 / 历史回显共用）。"""

    id: str = Field(..., description="反馈唯一 ID")
    message_id: str = Field(..., description="被评价的消息 ID")
    rating: str = Field(..., description="评价结论：up / down")
    reason: Optional[str] = Field(default=None, description="点踩归因（点赞为空）")
    comment: Optional[str] = Field(default=None, description="补充说明")
    created_at: str = Field(..., description="首次提交时间（ISO 8601）")
    updated_at: str = Field(..., description="最近修改时间（ISO 8601）")


class FeedbackStateResponse(BaseModel):
    """GET /messages/{message_id}/feedback 的响应。

    feedback 为 None 表示「当前用户还没评价过这条回答」——
    前端据此把点赞/点踩按钮置为未选中。
    """

    message_id: str = Field(..., description="消息 ID")
    feedback: Optional[FeedbackOut] = Field(default=None, description="当前用户的反馈（可为空）")


class FeedbackDownItem(BaseModel):
    """管理员评估视图中的一条差评（含上下文，便于定位问题）。"""

    feedback_id: str = Field(..., description="反馈 ID")
    message_id: str = Field(..., description="被评价的回答消息 ID")
    conversation_id: str = Field(..., description="所属会话 ID")
    question: str = Field("", description="该回答对应的用户提问（取同会话上一条用户消息）")
    answer: str = Field("", description="回答正文摘要（已截断）")
    reason: Optional[str] = Field(default=None, description="点踩归因")
    comment: Optional[str] = Field(default=None, description="用户补充说明")
    created_at: str = Field(..., description="提交时间（ISO 8601）")


class FeedbackSummaryResponse(BaseModel):
    """GET /admin/feedback/summary 的响应：答案质量评估概览。

    《需求清单》P6 功能2 把反馈定位为「用于后续 prompt/检索离线优化」，
    因此除了计数，还给出归因分布与最近的差评明细——这才是可行动的评估数据。
    """

    total: int = Field(0, description="反馈总数")
    up_count: int = Field(0, description="点赞数")
    down_count: int = Field(0, description="点踩数")
    # 好评率（0~1）：分母为 0 时记 0，避免除零
    up_rate: float = Field(0.0, description="好评率（0~1，无数据时为 0）")
    # 归因分布：{off_topic: 3, wrong: 1, ...}，只统计点踩且填写了归因的记录
    reason_breakdown: dict = Field(
        default_factory=dict, description="点踩归因分布（键为归因代码，值为条数）"
    )
    recent_down: List[FeedbackDownItem] = Field(
        default_factory=list, description="最近的差评明细（按时间倒序）"
    )


# ---------------------------------------------------------------------
# 前向引用解析（P6）
# ---------------------------------------------------------------------
# ConversationMessageOut 在 P6 之前定义，其 feedback 字段引用了本文件末尾的 FeedbackOut，
# 因此需要显式 rebuild 一次，让 Pydantic 在模块加载完成后解析该引用。
# 若不调用，FastAPI 在生成 OpenAPI / 校验响应时会报
# "FeedbackOut is not defined"（Pydantic v2 对未解析前向引用直接报错）。
ConversationMessageOut.model_rebuild()
ConversationDetail.model_rebuild()
