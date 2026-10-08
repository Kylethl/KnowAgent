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
