import { requestJson } from './http';
import type {
  NotificationList,
  NotificationReadAllResult,
  NotificationReadResult,
  NotificationUnread,
} from '../types';

// P6-F1：审核通知（站内信）接口
// ---------------------------------------------------------------------
// 职责：封装 /notifications 四个接口，供顶栏铃铛（NotificationBell）
// 与 useNotifications 轮询钩子调用。
//
// 设计要点：
//   1. 轮询只调 getUnreadCount（一个数字），避免每 30 秒拉一遍正文；
//   2. 完整列表只在「用户打开铃铛抽屉」时才拉，点击即拉最新；
//   3. 标记已读接口都回传最新未读数，前端无需再补一次请求。
//
// 鉴权：全部要求登录，令牌由 http.requestJson 自动注入；
//       通知只按 JWT 里的 user_id 查询，前端不传任何身份参数。

/**
 * 获取通知列表（默认取最新 20 条）。
 * @param params.unreadOnly 是否只看未读
 * @param params.limit 每页条数（后端限制 1~50）
 * @param params.offset 偏移量
 */
export async function listNotifications(
  params: { unreadOnly?: boolean; limit?: number; offset?: number } = {}
): Promise<NotificationList> {
  // 手工拼 query：只在有值时拼入，避免把 undefined 传成字符串 "undefined"
  const query = new URLSearchParams();
  if (params.unreadOnly) query.set('unread_only', 'true');
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.offset !== undefined) query.set('offset', String(params.offset));

  const qs = query.toString();
  return requestJson<NotificationList>(`/notifications${qs ? `?${qs}` : ''}`);
}

/** 只取未读数（顶栏角标轮询用，最省流量的接口）。 */
export async function getUnreadCount(): Promise<NotificationUnread> {
  return requestJson<NotificationUnread>('/notifications/unread-count');
}

/**
 * 标记单条通知已读（幂等）。
 * @param notificationId 通知 ID
 */
export async function markNotificationRead(
  notificationId: string
): Promise<NotificationReadResult> {
  const id = encodeURIComponent(notificationId);
  return requestJson<NotificationReadResult>(`/notifications/${id}/read`, { method: 'POST' });
}

/** 全部标记已读（一键清空角标）。 */
export async function markAllNotificationsRead(): Promise<NotificationReadAllResult> {
  return requestJson<NotificationReadAllResult>('/notifications/read-all', { method: 'POST' });
}
