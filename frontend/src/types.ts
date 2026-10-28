// 聊天消息相关类型定义（P1-F1 骨架共用）

// 消息角色：用户 / 助手 / 系统
export type MessageRole = 'user' | 'assistant' | 'system';

// 单条聊天消息的数据结构
export interface ChatMessage {
  id: string; // 消息唯一标识
  role: MessageRole; // 发送方角色
  content: string; // 消息文本内容
  createdAt: number; // 创建时间戳（毫秒）
  // P4-F1：本轮回答对应的知识库检索状态（仅助手消息、且后端下发过 retrieval 帧时有值）
  retrieval?: RetrievalInfo;
  // P4-F2：本轮回答的引用来源（后端 citations 帧 / 历史消息回显）
  citations?: Citation[];
  // P6-F2：后端落库后的真实消息 ID（messages.id）。
  // 为什么需要单独一个字段：流式渲染时气泡用的是前端临时 ID（见 useChatStream 的 genId），
  // 而反馈接口 POST /messages/{id}/feedback 必须用数据库主键，
  // 因此后端在落库后下发 {"type":"message","id":...} 帧，前端再回填到这里。
  // 历史消息（来自 GET /conversations/{id}）的 id 本身就是后端 ID，故 serverId 直接等于 id。
  serverId?: string;
  // P6-F2：当前用户对该回答的反馈（未评价为 undefined/null）
  feedback?: FeedbackItem | null;
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

// 文档类型：word 对应 .doc/.docx，markdown 对应 .md，
// P5 新增 image / audio / video（图片 / 音频 / 视频）
// 与后端 documents.file_type 字段严格对齐（P5 已在 CHECK 约束中放开这三类）
export type DocType = 'word' | 'markdown' | 'image' | 'audio' | 'video';

// P5：多模态类型（图片 / 音频 / 视频）
export type MediaKind = 'image' | 'audio' | 'video';

// P5：OCR/ASR 解析元信息（后端 documents.extract_meta 的下发结构）
// 字段全部可选：文档类没有解析元信息，历史数据也可能为空。
export interface ExtractMeta {
  kind?: string; // 媒体种类：image / audio / video
  provider?: string; // 解析服务提供方（如 qwen-compatible）
  model?: string; // 使用的模型名（qwen3.5-ocr / qwen3.8-omni-flash）
  chars?: number; // 提取出的字符数（判断解析质量的最直观指标）
  elapsed_ms?: number; // 解析耗时（毫秒）
  duration_seconds?: number | null; // 音视频时长（秒）
  segments?: number; // 音频切片段数（分段转写）
  extractor?: string; // 音轨提取方式：ffmpeg / none
  ocr_frames?: number; // 视频抽帧 OCR 的帧数
}

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
  // P4-F2：助手消息的引用来源（旧消息或未启用检索时为空数组）
  citations: Citation[];
  // P6-F2：当前用户对该回答的反馈（未评价为 null）
  feedback?: FeedbackItem | null;
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
  // ---- 入库状态自愈（僵死任务判定，由后端计算下发） ----
  // true 表示 status='indexing' 但已超过阈值仍未完成：上次入库被中断（进程重启/worker 被杀），
  // 任务不会自行结束。前端据此**放开**「重新入库」按钮，否则文档永久卡住、只剩预览可用。
  // 注意：阈值按文档类型不同（媒体类更宽松），故必须用后端判定结果，不要在前端用时间差自己算。
  indexing_stale: boolean;
  indexing_elapsed_seconds: number | null; // 入库中已持续秒数（非入库中为 null）
  // ---- P4：向量时效性（防 mock 向量 / 旧模型向量混入检索） ----
  embedding_model: string | null; // 入库时使用的向量模型签名（mock 或模型名）；历史数据为 null
  embedding_stale: boolean; // true 表示向量已过期（与当前模型不可比），建议重新入库
  // ---- P5：多模态解析元信息（图片/音视频入库后才有值） ----
  extract_meta: ExtractMeta | null; // OCR/ASR 解析元信息；文档类为 null
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
  extract_meta: ExtractMeta | null; // P5：OCR/ASR 解析元信息（文档类为 null）
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
  // ---- P4：向量时效性（用户侧也需知道「为什么我的文档检索不到」） ----
  embedding_model: string | null; // 入库时使用的向量模型签名（mock 或模型名）
  embedding_stale: boolean; // true 表示向量已过期，需管理员重新入库后才能参与检索
  // ---- P5：多模态解析元信息（图片/音视频入库后才有值） ----
  extract_meta: ExtractMeta | null; // OCR/ASR 解析元信息；文档类为 null
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

// =====================================================================
// P4-F1：RAG 检索增强相关类型
// 契约来源：《需求清单》P4 功能1「RAG 增强回答可见」
//   - 前端：在 P1 对话页基础上接入检索增强渲染（引用区块先占位，见功能2）
//   - 后端：/chat 检索增强；SSE 额外下发 {"type":"retrieval",...} 帧
// 四种 mode 与后端 services/retriever.py 的常量严格一致，不可随意增删。
// =====================================================================

// rag      正常命中知识库，回答基于检索资料
// no_hit   检索已执行但无相关切片，回答为通用回答
// disabled 检索未启用（配置关闭 / 未配置可用向量模型 / 无数据库）
// fallback 检索过程出错，已降级为通用回答
export type RetrievalMode = 'rag' | 'no_hit' | 'disabled' | 'fallback';

// 一次检索的元信息（后端 RetrievalResult.meta_payload() 的下发结构）
export interface RetrievalInfo {
  mode: RetrievalMode; // 检索结果模式
  count: number; // 命中片段数（mode=rag 时 > 0）
  cached?: boolean; // 是否命中后端缓存（排查性能时参考）
  elapsed_ms?: number; // 后端检索耗时（毫秒）
  reason?: string; // 未命中 / 未启用 / 降级的原因
}

// =====================================================================
// P4-F2：答案引用来源相关类型
// 契约来源：《需求清单》P4 功能2「答案引用来源」
//   - 前端：回答下方展示引用文档卡片（标题、片段、可跳转）
//   - 后端：/chat 返回 cite 列表（doc_id + chunk 片段）；messages 存引用关系
// 字段与后端 CitationOut（app/schemas.py）严格一致，SSE 帧与历史消息共用同一结构。
// =====================================================================

// 单条引用来源
export interface Citation {
  index: number; // 资料编号，对应回答正文中的 [n] 标注
  doc_id: string; // 来源文档 ID（跳转原文用）
  doc_title: string; // 来源文档标题（原文件名）
  file_type: string; // 文档类型：word / markdown
  chunk_id: string; // 切片 ID（跳转原文片段用）
  chunk_index: number; // 切片在文档内的序号（从 0 开始）
  heading: string; // 所属章节标题（可为空）
  snippet: string; // 片段摘要（卡片展示）
  score: number; // 综合排序分
  vector_score: number; // 语义相似度（0~1，展示「相关度」更直观）
  cited: boolean; // true=回答直接引用了该段；false=仅作为参考资料注入
}

// 引用片段详情（GET /documents/{doc_id}/chunks/{chunk_id} 的响应）
// 「可跳转」的落地：点引用卡片即拉取该段完整正文，并支持上一段/下一段查看上下文。
export interface ChunkDetail {
  doc_id: string; // 文档 ID
  doc_title: string; // 文档标题
  file_type: string; // 文档类型
  chunk_id: string; // 切片 ID
  chunk_index: number; // 切片序号（从 0 开始）
  total_chunks: number; // 该文档切片总数（用于「第 n/N 段」）
  heading: string; // 所属章节
  content: string; // 切片完整正文
  prev_chunk_id: string | null; // 上一段切片 ID（首段为 null）
  next_chunk_id: string | null; // 下一段切片 ID（末段为 null）
}

// =====================================================================
// P6-F1：审核通知（站内信）相关类型
// 契约来源：《需求清单》P6 功能1「审核通知」
//   - 前端：站内信 / Toast 提示「文档已通过 / 被驳回」
//   - 后端：状态变更触发通知；接口 /notifications（列表 / 未读数 / 标记已读）
// 字段与后端 NotificationOut / NotificationListResponse 严格对齐（不驼峰转换）。
// =====================================================================

// 通知类型：审核通过 / 审核驳回（与 notifications.type 的 CHECK 约束一致）
// document_approved 文档已通过审核，正在入库
// document_rejected 文档被驳回（content 内含驳回理由）
export type NotificationType = 'document_approved' | 'document_rejected';

// 单条站内信
export interface NotificationItem {
  id: string; // 通知唯一 ID
  type: NotificationType; // 通知类型（决定图标与配色）
  title: string; // 标题（一句话结论）
  content: string; // 正文（含文件名与驳回理由等细节）
  doc_id: string | null; // 关联文档 ID（文档被清理后为 null）
  read_at: string | null; // 已读时间（ISO 8601；null 表示未读）
  created_at: string; // 创建时间（ISO 8601）
}

// GET /notifications 响应：当前页 + 总数 + 未读数
export interface NotificationList {
  items: NotificationItem[]; // 当前页通知
  total: number; // 满足筛选条件的总条数
  unread_count: number; // 未读总数（不受 unread_only 影响）
  limit: number; // 每页条数
  offset: number; // 当前偏移量
}

// GET /notifications/unread-count 响应：轮询角标专用
export interface NotificationUnread {
  unread_count: number;
}

// POST /notifications/{id}/read 响应：通知本体 + 最新未读数
export interface NotificationReadResult {
  notification: NotificationItem;
  unread_count: number;
}

// POST /notifications/read-all 响应
export interface NotificationReadAllResult {
  updated: number; // 本次标记为已读的条数
  unread_count: number; // 标记后的未读数（恒为 0）
}

// =====================================================================
// P6-F2：答案反馈与评估相关类型
// 契约来源：《需求清单》P6 功能2「答案反馈与评估」
//   - 前端：回答点赞/点踩按钮 + 反馈表单
//   - 后端：POST /messages/{id}/feedback 存储反馈；/admin/feedback/summary 供评估
// 字段与后端 FeedbackOut / FeedbackStateResponse / FeedbackSummaryResponse 严格对齐。
// =====================================================================

// 评价结论：与 feedback.rating 的 CHECK 约束一致
export type FeedbackRating = 'up' | 'down';

// 点踩归因：与 feedback.reason 的 CHECK 约束一致
// off_topic    答非所问
// wrong        内容错误
// unsupported  资料不符（引用与原文对不上）
// other        其他
export type FeedbackReason = 'off_topic' | 'wrong' | 'unsupported' | 'other';

// 单条反馈（提交结果 / 历史回显共用）
export interface FeedbackItem {
  id: string; // 反馈 ID
  message_id: string; // 被评价的消息 ID
  rating: FeedbackRating; // 评价结论
  reason: FeedbackReason | null; // 点踩归因（点赞为 null）
  comment: string | null; // 补充说明
  created_at: string; // 首次提交时间（ISO 8601）
  updated_at: string; // 最近修改时间（ISO 8601）
}

// POST /messages/{id}/feedback 请求体
// 注意：不传 message_id / user_id——前者在路径里，后者由后端从 JWT 解析
export interface FeedbackPayload {
  rating: FeedbackRating;
  reason?: FeedbackReason | null; // 点踩归因（可选）
  comment?: string | null; // 补充说明（可选，最多 1000 字）
}

// GET /messages/{id}/feedback 响应
export interface FeedbackState {
  message_id: string;
  feedback: FeedbackItem | null; // 当前用户的反馈；null 表示未评价
}

// 管理员评估视图中的一条差评（含上下文）
export interface FeedbackDownEntry {
  feedback_id: string;
  message_id: string;
  conversation_id: string;
  question: string; // 该回答对应的用户提问
  answer: string; // 回答摘要（已截断）
  reason: FeedbackReason | null;
  comment: string | null;
  created_at: string;
}

// GET /admin/feedback/summary 响应：答案质量评估概览
export interface FeedbackSummary {
  total: number;
  up_count: number;
  down_count: number;
  up_rate: number; // 好评率（0~1，无数据为 0）
  reason_breakdown: Record<string, number>; // 点踩归因分布
  recent_down: FeedbackDownEntry[]; // 最近差评明细
}
