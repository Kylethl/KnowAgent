import { requestVoid, requestJson } from './http';
import type {
  FeedbackItem,
  FeedbackPayload,
  FeedbackState,
  FeedbackSummary,
} from '../types';

// P6-F2：答案反馈接口
// ---------------------------------------------------------------------
// 契约来源：《需求清单》P6 功能2「答案反馈与评估」
//   - 前端：回答点赞/点踩按钮 + 反馈表单
//   - 后端：/messages/{id}/feedback（提交 / 查询 / 取消）
//
// 关键约定：
//   1. messageId 必须是**后端真实消息 ID**（messages.id），
//      不是前端流式渲染用的临时 ID（见 types.ts 中 ChatMessage.serverId）；
//   2. 重复提交同一 messageId 视为修改（后端 upsert），因此「先赞后踩」无需先删除；
//   3. 取消反馈走 DELETE，幂等（本来没评价也返回 204）。

/**
 * 提交或修改一条反馈。
 * @param messageId 后端消息 ID（助手回答）
 * @param payload 评价结论 + 可选归因/备注
 */
export async function submitFeedback(
  messageId: string,
  payload: FeedbackPayload
): Promise<FeedbackItem> {
  const id = encodeURIComponent(messageId);
  return requestJson<FeedbackItem>(`/messages/${id}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/**
 * 查询当前用户对某条回答的反馈（刷新后恢复按钮选中状态）。
 * @param messageId 后端消息 ID
 */
export async function getFeedback(messageId: string): Promise<FeedbackState> {
  const id = encodeURIComponent(messageId);
  return requestJson<FeedbackState>(`/messages/${id}/feedback`);
}

/**
 * 取消反馈（点错了可撤销）。
 * @param messageId 后端消息 ID
 */
export async function deleteFeedback(messageId: string): Promise<void> {
  const id = encodeURIComponent(messageId);
  return requestVoid(`/messages/${id}/feedback`, { method: 'DELETE' });
}

/**
 * 答案质量评估概览（管理员）。
 * @param recentLimit 最近差评返回条数（后端限制 1~50）
 */
export async function getFeedbackSummary(recentLimit = 10): Promise<FeedbackSummary> {
  const qs = new URLSearchParams({ recent_limit: String(recentLimit) }).toString();
  return requestJson<FeedbackSummary>(`/admin/feedback/summary?${qs}`);
}

// 归因代码 → 中文文案：前后端共用的唯一映射来源（后端存代码，前端展示文案）。
// 放在这里而不是组件里，是为了让「按钮标签」「管理员统计」「历史回显」用同一套措辞。
export const FEEDBACK_REASON_LABELS: Record<string, string> = {
  off_topic: '答非所问',
  wrong: '内容有误',
  unsupported: '资料不符',
  other: '其他问题',
};
