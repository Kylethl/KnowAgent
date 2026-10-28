import { useCallback, useEffect, useRef, useState } from 'react';
import type { NotificationItem } from '../types';
import {
  getUnreadCount,
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from '../services/notificationApi';

// P6-F1：审核通知轮询钩子
// ---------------------------------------------------------------------
// 职责：为顶栏铃铛提供「未读数 / 通知列表 / 标记已读」，并识别**新到达**的通知
//       交给上层弹 Toast（《需求清单》P6 功能1「站内信 / Toast 提示」）。
//
// 轮询策略（成本与实时性的折中）：
//   1. 每 POLL_INTERVAL_MS 只请求 /notifications/unread-count（一个数字），
//      不会为了角标把正文反复拉一遍；
//   2. 只有未读数**变大**时才去拉一次列表——说明有新通知到了，这时才需要内容；
//   3. 页面重新可见（切回标签页）时立即补一次，避免后台标签页定时器被节流导致长时间不刷新。
//
// 为什么不在 SSE 流里推通知：
//   通知的产生时机（管理员审核）与用户当前是否在对话无关，
//   为它单独维持一条长连接成本过高；30 秒轮询对「审核结果」这类低频事件完全够用。
//
// Toast 去重：
//   用 knownIdsRef 记录「已经见过的通知 ID」。首次加载只建立基线**不弹窗**——
//   否则用户一进站就会被历史上十几条未读通知糊一脸。
//   之后只有不在已知集合里的 ID 才进入 pendingToasts，由组件弹一次后清空。

// 轮询间隔：30 秒。审核是低频操作，更短没有收益，反而增加无效请求。
const POLL_INTERVAL_MS = 30_000;

// 列表一次拉取条数：与后端默认值一致（下拉面板展示最新 20 条足够）
const LIST_LIMIT = 20;

export interface UseNotificationsResult {
  unread: number; // 未读数（角标）
  items: NotificationItem[]; // 最新一页通知（下拉面板）
  loading: boolean; // 列表加载中
  error: string | null; // 最近一次加载失败的原因
  pendingToasts: NotificationItem[]; // 待弹 Toast 的新通知（组件弹完调用 dismissToasts）
  refresh: () => Promise<void>; // 手动刷新列表与角标
  markRead: (id: string) => Promise<void>; // 标记单条已读
  markAllRead: () => Promise<void>; // 全部标记已读
  dismissToasts: () => void; // 清空待弹队列（避免重复弹窗）
}

export function useNotifications(enabled = true): UseNotificationsResult {
  const [unread, setUnread] = useState(0);
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingToasts, setPendingToasts] = useState<NotificationItem[]>([]);

  // 已知通知 ID（用于识别「新到达」）；ref 而非 state：变更不需要触发渲染
  const knownIdsRef = useRef<Set<string>>(new Set());
  // 是否已完成首次基线（首次加载不弹 Toast）
  const baselinedRef = useRef(false);
  // 上一次的未读数：用于判断「是否变大」（变大才拉列表）
  const unreadRef = useRef(0);
  // 组件是否仍挂载：异步回调里避免对已卸载组件 setState
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  /**
   * 拉取通知列表并同步角标，同时挑出「新到达」的通知放进待弹队列。
   *
   * 说明：固定拉**全部**（而非只拉未读）。只拉未读会让面板在轮询后
   * 「丢掉已读的历史条目」，用户刚点完一条通知，列表就少一条，观感很怪。
   * 一次列表请求多带几百字节，换取面板内容稳定，值得。
   */
  const refreshList = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await listNotifications({ limit: LIST_LIMIT });
      if (!mountedRef.current) return;

      setItems(res.items);
      setUnread(res.unread_count);
      unreadRef.current = res.unread_count;

      // 首次加载：只建立基线，不弹窗（否则历史未读会一次性糊满屏幕）
      if (!baselinedRef.current) {
        res.items.forEach((n) => knownIdsRef.current.add(n.id));
        baselinedRef.current = true;
        return;
      }

      // 之后：挑出没见过的通知（时间正序弹出，符合阅读顺序）
      const fresh = res.items
        .filter((n) => !knownIdsRef.current.has(n.id))
        .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
      if (fresh.length > 0) {
        fresh.forEach((n) => knownIdsRef.current.add(n.id));
        setPendingToasts((prev) => [...prev, ...fresh]);
      }
    } catch (err) {
      if (!mountedRef.current) return;
      // 通知加载失败不应打断任何主流程（对话/上传都不依赖它），只记录并在面板里提示
      setError(err instanceof Error ? err.message : '加载通知失败');
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  /**
   * 轮询：先问未读数，只有变大时才拉列表（新通知需要正文才能弹 Toast）。
   * 未读数变小（如在别处已读）时只同步角标，不拉列表。
   */
  const poll = useCallback(async () => {
    try {
      const res = await getUnreadCount();
      if (!mountedRef.current) return;

      const prev = unreadRef.current;
      setUnread(res.unread_count);
      unreadRef.current = res.unread_count;

      if (res.unread_count > prev) {
        // 有新通知：拉一次列表拿到标题/正文（同时触发 Toast 判定）
        await refreshList();
      }
    } catch {
      // 轮询失败（网络抖动/后端重启）静默忽略：下一轮会自己恢复，
      // 不弹错误提示，否则用户会周期性看到无意义的红条
    }
  }, [refreshList]);

  // 首次挂载 + 轮询 + 可见性恢复
  useEffect(() => {
    if (!enabled) return;

    // 首屏：拉一次完整列表（建立基线 + 填满下拉面板）
    void refreshList();

    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);

    // 页面从后台切回：立刻补一次（后台标签页的定时器常被浏览器节流到分钟级）
    const onVisible = () => {
      if (document.visibilityState === 'visible') void poll();
    };
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [enabled, refreshList, poll]);

  /**
   * 手动刷新：拉完整列表（用户展开铃铛面板时调用，保证看到最新状态）。
   */
  const refresh = useCallback(async () => {
    await refreshList();
  }, [refreshList]);

  /**
   * 标记单条已读：后端返回最新未读数，本地同步该条目的 read_at，
   * 避免为了一个红点再拉一次列表。
   */
  const markRead = useCallback(async (id: string) => {
    // 已是已读则跳过：避免悬停/点击时产生无意义的写请求
    const target = items.find((n) => n.id === id);
    if (target?.read_at) return;

    try {
      const res = await markNotificationRead(id);
      if (!mountedRef.current) return;

      setItems((prev) =>
        prev.map((n) => (n.id === id ? res.notification : n))
      );
      setUnread(res.unread_count);
      unreadRef.current = res.unread_count;
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err instanceof Error ? err.message : '标记已读失败');
    }
  }, [items]);

  /**
   * 全部标记已读：一键清空角标，并把面板里所有条目置为已读。
   */
  const markAllRead = useCallback(async () => {
    try {
      await markAllNotificationsRead();
      if (!mountedRef.current) return;

      setUnread(0);
      unreadRef.current = 0;
      // 用本地时间填充 read_at 即可：只为渲染「已读」灰度，不需要服务端精确时间
      const now = new Date().toISOString();
      setItems((prev) => prev.map((n) => (n.read_at ? n : { ...n, read_at: now })));
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err instanceof Error ? err.message : '全部标为已读失败');
    }
  }, []);

  /** 清空待弹队列：组件弹完 Toast 后调用，避免下次渲染重复弹。 */
  const dismissToasts = useCallback(() => setPendingToasts([]), []);

  return {
    unread,
    items,
    loading,
    error,
    pendingToasts,
    refresh,
    markRead,
    markAllRead,
    dismissToasts,
  };
}
