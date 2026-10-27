// 聊天消息相关类型定义（P1-F1 骨架共用）

// 消息角色：用户 / 助手 / 系统
export type MessageRole = 'user' | 'assistant' | 'system';

// 单条聊天消息的数据结构
export interface ChatMessage {
  id: string; // 消息唯一标识
  role: MessageRole; // 发送方角色
  content: string; // 消息文本内容
  createdAt: number; // 创建时间戳（毫秒）
}

// P1-F2：对话流式状态
// idle      空闲，可发送
// thinking  已发送、等待首个分片（加载/停顿态）
// streaming 正在流式输出（打字机渲染中）
// error     流式中断或失败
export type StreamStatus = 'idle' | 'thinking' | 'streaming' | 'error';

// =====================================================================
// P1-F3：文档上传相关类型
// 契约来源：《需求清单》P1 功能2「用户提交文档（仅 Word / Markdown）」
//   - 前端：上传入口（拖拽/选择），仅接受 .docx/.doc/.md，显示上传中状态与「待审核」标签
//   - 数据存储：documents 表（id, user_id, filename, type=word/markdown, status=待审核, created_at）
// =====================================================================

// 文档类型：word 对应 .doc/.docx，markdown 对应 .md
// 与后端 documents.type 字段（P1-DB2 建表）严格对齐，不可随意扩展（P5 再扩 image/audio/video）
export type DocType = 'word' | 'markdown';

// 文档在知识库中的生命周期状态
// 取值与后端 documents.status 的 CHECK 约束严格一致
// （见 backend/sql/002_p1_db2_documents.sql：pending/approved/rejected/indexing/indexed）
// pending   待审核：P1 提交后的初始状态，等待管理员审核（P3-F1）
// approved  审核通过（P3-B1 写入）
// rejected  审核驳回（P3-B1 写入）
// indexing  入库中：正在解析切片与向量化（P3-B2 写入）
// indexed   已入库：可参与 RAG 检索（P3-B2 写入）
export type DocStatus = 'pending' | 'approved' | 'rejected' | 'indexing' | 'indexed';

// 单条文档记录：后端 /documents/upload 成功返回的结构（前端只读展示）
// 字段名与后端 documents 表（及 Document ORM）逐一对齐，不做驼峰转换，
// 避免前后端多次映射出错：id / filename / file_type / status / size_bytes / original_path / created_at
export interface DocumentRecord {
  id: string; // 文档唯一 ID（后端 UUID）
  filename: string; // 原始文件名（保留用户看到的名字，与存储落盘名无关）
  file_type: DocType; // 文档类型（后端列名 file_type）
  status: DocStatus; // 文档状态；P1 上传成功固定为 pending（待审核）
  size_bytes: number; // 文件字节数（后端列名 size_bytes）
  original_path: string; // 对象存储相对路径（后端列名 original_path），禁止直接拼接成下载链接
  mime_type?: string; // MIME 类型，可选（便于后续预览/下载设置 Content-Type）
  created_at: string; // 提交时间（ISO 8601 字符串）
}

// 上传队列单项的本地状态（前端视角，区别于文档在库中的 DocStatus）
// uploading 传输中（含等待后端响应）
// success   后端已接收并入库，等待审核
// error     传输失败或被后端拒绝
export type UploadItemStatus = 'uploading' | 'success' | 'error';

// 上传队列中的一项：本地文件 + 实时进度 + 服务端回执
export interface UploadItem {
  id: string; // 本地唯一 ID（非后端 ID，后端 ID 在 record.id 中）
  file: File; // 原始 File 对象，重试时需要复用
  docType: DocType; // 由扩展名推导出的文档类型
  status: UploadItemStatus; // 当前上传状态
  percent: number; // 上传进度百分比（0-100）
  error?: string; // 失败原因（status=error 时有值）
  record?: DocumentRecord; // 成功后服务端返回的文档记录
}

// =====================================================================
// P2-F1：对话历史与删除相关类型
// 契约来源：《需求清单》P2 功能1「对话历史与删除」+ 后端 app/api/conversations.py
//   - 前端：左侧会话列表（新建/选中/删除）+ 切换会话续聊保持上下文
//   - 数据存储：conversations / messages 表，按 user_id 隔离（P2-DB1）
// 字段命名与后端 Conversation* 响应模型严格对齐（id / title / model /
// created_at / updated_at / message_count / messages），避免多次映射出错。
// =====================================================================

// 会话列表项：基础字段 + 消息条数
export interface ConversationSummary {
  id: string; // 会话唯一 ID（后端 UUID）
  title: string | null; // 会话标题（首条问题自动生成，可能为空）
  model: string; // 对话使用的模型（DeepSeek-Flash）
  created_at: string; // 创建时间（ISO 8601）
  updated_at: string; // 更新时间（ISO 8601，最近活跃排序用）
  message_count: number; // 该会话下的消息条数
}

// 会话详情：基础字段 + 历史消息列表
export interface ConversationDetail extends ConversationSummary {
  messages: ConversationMessage[]; // 历史消息（按时间正序）
}

// 单条历史消息：对应 messages 表的一行
export interface ConversationMessage {
  id: string; // 消息唯一 ID
  role: MessageRole; // 角色：user / assistant / system
  content: string; // 消息正文
  created_at: string; // 发送时间（ISO 8601）
}

// =====================================================================
// P2-F2：用户鉴权相关类型
// 契约来源：《需求清单》P2 功能2「用户鉴权与登录」+ 后端 app/api/auth.py
//   - 前端：登录/注册页、token 存储（localStorage）、路由守卫
//   - 后端：JWT 鉴权、注册/登录、密码 bcrypt 加密；users 表预留 role
// 字段与后端 UserOut / TokenResponse 严格对齐（不返回 password_hash 等敏感字段）。
// =====================================================================

// 用户角色：普通用户 / 管理员
// 与后端 users.role 的 CHECK 约束一致（user/admin）；admin 供 P3 管理后台 RBAC 使用
export type AuthRole = 'user' | 'admin';

// 登录用户（脱敏视图）
export interface AuthUser {
  id: string; // 用户唯一 ID（后端 UUID）
  username: string; // 登录名
  email: string | null; // 邮箱（可为空）
  role: AuthRole; // 角色
  created_at: string; // 注册时间（ISO 8601）
}

// 登录成功响应：令牌 + 有效期 + 用户信息
export interface LoginResult {
  access_token: string; // JWT 访问令牌
  token_type: string; // 固定 bearer
  expires_in: number; // 有效期（秒）
  user: AuthUser; // 当前登录用户
}

// =====================================================================
// P3-F1：管理员审核相关类型
// 契约来源：《需求清单》P3 功能1「管理员审核」
//   - 前端：管理员后台页、待审核列表、文档预览、通过/驳回（驳回必填理由）
//   - 后端：GET /admin/documents、POST /admin/documents/{id}/review、
//          GET /admin/documents/{id}/preview（全部 require_admin）
// 字段与后端 AdminDocumentOut / ReviewResult / DocumentPreviewResponse 严格对齐。
// =====================================================================

// 审核结论：与后端 documents.review_status 的 CHECK 约束一致（P3-DB1 新增列）
// pending   待审核
// approved  审核通过
// rejected  审核驳回
export type ReviewStatus = 'pending' | 'approved' | 'rejected';

// 管理员视角的文档条目（审核队列列表项）
export interface AdminDocument {
  id: string; // 文档唯一 ID
  filename: string; // 原始文件名
  file_type: DocType; // 文档类型 word / markdown
  status: DocStatus; // 生命周期状态（pending/approved/rejected/indexing/indexed）
  review_status: ReviewStatus; // 审核结论（审核队列据此筛选）
  size_bytes: number; // 文件字节数
  uploader: string | null; // 提交人用户名（已注销为 null）
  reviewer: string | null; // 审核人用户名（未审核为 null）
  review_note: string | null; // 审核备注（驳回理由）
  reviewed_at: string | null; // 审核时间（ISO 8601）
  created_at: string; // 提交时间（ISO 8601）
  // ---- P3-B2 入库状态字段（供管理员判断入库结果） ----
  chunk_count: number; // 已入库切片数（0 表示未入库/已下线）
  indexed_at: string | null; // 入库完成时间（ISO 8601）
  last_index_error: string | null; // 最近一次入库失败原因（成功为空）
}

// GET /admin/documents 响应：当前页列表 + 总数（分页用）
export interface AdminDocumentList {
  items: AdminDocument[]; // 当前页文档列表
  total: number; // 满足条件的总条数
  limit: number; // 每页条数
  offset: number; // 当前偏移量
}

// POST /admin/documents/{id}/review 请求体
export interface ReviewPayload {
  action: 'approved' | 'rejected'; // 审核动作：通过 / 驳回
  note?: string; // 审核备注；驳回时必填理由（后端二次校验）
}

// POST /admin/documents/{id}/review 响应：回传更新后的文档，便于前端就地更新表格行
export interface ReviewResultData {
  document: AdminDocument;
}

// GET /admin/documents/{id}/preview 响应：文本预览
export interface DocumentPreview {
  id: string; // 文档唯一 ID
  filename: string; // 原始文件名
  file_type: DocType; // 文档类型
  text: string; // 预览文本（可能已截断）
  truncated: boolean; // 是否因超长被截断
  previewable: boolean; // 是否成功提取出文本（false 时 text 为提示文案）
}

// =====================================================================
// P3-B2：文档入库（RAG 管线）前端相关类型
// 契约来源：《需求清单》P3 功能2「文档入库（RAG 管线）」
//   - 前端：上传面板「我的文档」展示本人文档状态；管理员后台支持重新入库/下线
//   - 后端：GET /documents（我的文档）、POST /admin/documents/{id}/index（重入库）、
//          POST /admin/documents/{id}/unpublish（下线）
// 字段与后端 MyDocumentOut / IndexResult 严格对齐（不驼峰转换，避免映射出错）。
// =====================================================================

// 用户视角的「我的文档」条目
// status 取值：pending/approved/rejected/indexing/indexed
// ⚠️ 入库失败并不会产生独立的 status 值，而是回退为 'approved' 并写入 last_index_error，
//    因此前端需结合 last_index_error 区分「已通过待入库」与「入库失败」。
export interface MyDocument {
  id: string; // 文档唯一 ID
  filename: string; // 原始文件名
  file_type: DocType; // 文档类型 word / markdown
  status: DocStatus; // 生命周期状态（indexed 即「已入库」）
  review_status: ReviewStatus; // 审核结论（rejected 时展示驳回理由）
  size_bytes: number; // 文件字节数
  chunk_count: number; // 已入库切片数（>0 表示已入库）
  review_note: string | null; // 驳回理由（被驳回时展示）
  last_index_error: string | null; // 最近一次入库失败原因（成功为空）
  created_at: string; // 提交时间（ISO 8601）
  indexed_at: string | null; // 入库完成时间（ISO 8601）
}

// GET /documents 响应：当前登录用户提交的文档列表
export interface MyDocumentList {
  items: MyDocument[]; // 当前用户的文档列表（按提交时间倒序）
  total: number; // 文档总条数
}

// 入库类操作（入库 / 重入库 / 下线）的统一响应
// queued=True 表示已投递 Celery 异步执行（前端应展示「入库中」）；
// queued=False 表示已在进程内同步执行（无 Redis 时的回退路径）。
export interface IndexResult {
  document: AdminDocument; // 操作后的文档（含最新入库状态）
  queued: boolean; // 是否投递到 Celery 异步执行
  message: string; // 可读的结果说明（便于前端提示）
}
