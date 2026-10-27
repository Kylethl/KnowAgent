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
    # 文档类型：word（.doc/.docx）/ markdown（.md）
    type: str = Field(..., description="文档类型：word / markdown")
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


class ConversationMessageOut(BaseModel):
    """历史消息项：对应 messages 表的一行。"""

    id: str = Field(..., description="消息唯一 ID")
    role: str = Field(..., description="角色：user / assistant / system")
    content: str = Field(..., description="消息正文")
    created_at: str = Field(..., description="发送时间（ISO 8601）")


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
    file_type: str = Field(..., description="文档类型：word / markdown")
    text: str = Field(..., description="预览文本（可能已截断）")
    truncated: bool = Field(..., description="是否因超长被截断")
    # 无法解析（如旧版 .doc）时给出说明，前端据此提示管理员下载查看
    previewable: bool = Field(..., description="是否成功提取出文本内容")


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


class MyDocumentListResponse(BaseModel):
    """GET /documents 的响应：当前登录用户提交的文档列表。"""

    items: List[MyDocumentOut] = Field(
        default_factory=list, description="当前用户的文档列表（按提交时间倒序）"
    )
    total: int = Field(..., description="文档总条数")
