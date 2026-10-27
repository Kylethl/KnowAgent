import type { ConversationDetail, ConversationSummary } from '../types';

// P2-F1：会话历史与删除 API 服务
// 职责：封装后端 /conversations 四个接口（列表/新建/历史/软删除），
//       统一走 /api 前缀（Vite 开发代理与 nginx 生产反代都会剥离 /api 前缀转发到 FastAPI）。
//
// 说明：当前为「无登录」阶段，后端自动落到 Demo 用户（见后端 core/security.py），
//       因此前端无需携带 user 信息即可联调；P2-B2 真实登录后将由前端携带 JWT，
//       本服务无需改动（请求仍走同一组接口）。

// 接口地址前缀：与 vite.config.ts 的代理约定一致，统一 /api 前缀
const API_PREFIX = import.meta.env.VITE_API_BASE ?? '/api';

// 统一错误处理：把后端返回的可读 detail 抽出来，便于上层提示
async function parseError(resp: Response): Promise<Error> {
  let detail = `请求失败（${resp.status}）`;
  try {
    const data = (await resp.json()) as { detail?: unknown };
    if (typeof data.detail === 'string') detail = data.detail;
  } catch {
    // 响应体非 JSON 时沿用默认文案
  }
  return new Error(detail);
}

/**
 * 获取当前用户的会话列表（按最近活跃倒序）。
 * @returns 会话摘要数组（含 message_count）
 */
export async function listConversations(): Promise<ConversationSummary[]> {
  const resp = await fetch(`${API_PREFIX}/conversations`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as ConversationSummary[];
}

/**
 * 新建一个会话。
 * @param title 可选标题；不传则由后端在首条消息到达时自动生成。
 * @returns 新建的会话摘要（含 id）
 */
export async function createConversation(title?: string): Promise<ConversationSummary> {
  const resp = await fetch(`${API_PREFIX}/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: title ?? null }),
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as ConversationSummary;
}

/**
 * 获取指定会话的历史消息（按时间正序），用于「选中会话回看 / 续聊」。
 * @param id 会话 ID
 * @returns 会话详情（含 messages）
 */
export async function getConversation(id: string): Promise<ConversationDetail> {
  const resp = await fetch(`${API_PREFIX}/conversations/${id}`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as ConversationDetail;
}

/**
 * 软删除会话（仅标记删除，不物理删除）。
 * @param id 会话 ID
 */
export async function deleteConversation(id: string): Promise<void> {
  const resp = await fetch(`${API_PREFIX}/conversations/${id}`, {
    method: 'DELETE',
  });
  if (!resp.ok) throw await parseError(resp);
  // 204 No Content：无需解析响应体
}
