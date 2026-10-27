import type { ConversationDetail, ConversationSummary } from '../types';
import { requestJson, requestVoid } from './http';

// P2-F1：会话历史与删除 API 服务
// 职责：封装后端 /conversations 四个接口（列表/新建/历史/软删除）。
//
// P2-F2 变更：
//   原来各接口自己拼 fetch 与错误处理；现在统一走 services/http.ts 的
//   requestJson / requestVoid，由它们自动附加 Authorization: Bearer <token>，
//   并在收到 401 时清理本地令牌、跳转登录页。本文件因此删掉了本地的
//   parseError 与 /api 前缀拼接逻辑，只保留「接口语义」。

/**
 * 获取当前用户的会话列表（按最近活跃倒序）。
 * @returns 会话摘要数组（含 message_count）
 */
export async function listConversations(): Promise<ConversationSummary[]> {
  return requestJson<ConversationSummary[]>('/conversations', {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });
}

/**
 * 新建一个会话。
 * @param title 可选标题；不传则由后端在首条消息到达时自动生成。
 * @returns 新建的会话摘要（含 id）
 */
export async function createConversation(title?: string): Promise<ConversationSummary> {
  return requestJson<ConversationSummary>('/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: title ?? null }),
  });
}

/**
 * 获取指定会话的历史消息（按时间正序），用于「选中会话回看 / 续聊」。
 * @param id 会话 ID
 * @returns 会话详情（含 messages）
 */
export async function getConversation(id: string): Promise<ConversationDetail> {
  return requestJson<ConversationDetail>(`/conversations/${id}`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });
}

/**
 * 软删除会话（仅标记删除，不物理删除）。
 * @param id 会话 ID
 */
export async function deleteConversation(id: string): Promise<void> {
  // 后端返回 204 No Content，故用 requestVoid 跳过 JSON 解析
  return requestVoid(`/conversations/${id}`, { method: 'DELETE' });
}
