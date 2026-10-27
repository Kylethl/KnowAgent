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
