// 管理员后台的行级业务规则（纯函数，无 React / antd 依赖）
// ---------------------------------------------------------------------
// 为什么单独抽出来：
//   这些规则原本内联在 AdminPage 的 Table render 里，只能靠人工点击验证。
//   历史上真实踩过的坑——「入库僵死」的文档按钮被 `status === 'indexing'` 一律禁用，
//   页面只显示「预览」，文档永远无法重新入库（用户 u_4ff45c 的《部署手册.md》）。
//   抽成纯函数后可以用 vitest 直接锁死这类「UI 门禁与业务语义不符」的缺陷，
//   不需要渲染组件、不需要起后端。
//
// 设计原则：
//   - 只依赖类型，不读全局状态、不发请求、不碰 DOM ⇒ 可确定性单测；
//   - 判定依据必须来自后端下发的字段（如 indexing_stale），
//     前端**不自行**用时间差推算阈值——阈值按文档类型不同，且会调整。

import type { AdminDocument } from '../types';

/**
 * 判断「入库中」是否已僵死（上次任务被中断，不会再自行完成）。
 *
 * 注意：这里刻意不自己算时间差，只信任后端下发的 indexing_stale。
 * 原因：僵死阈值按文档类型不同（文本 10 分钟 / 媒体 60 分钟），
 * 前端重算必然与后端不一致，出现「界面可点、接口 409」或反过来的割裂。
 *
 * @param row 管理员文档行
 * @returns true 表示已僵死
 */
export function isIndexingStuck(row: Pick<AdminDocument, 'status' | 'indexing_stale'>): boolean {
  return row.status === 'indexing' && row.indexing_stale === true;
}

/**
 * 「重新入库」按钮是否可点。
 *
 * 规则（必须同时满足）：
 *   1. 审核已通过 —— 未通过/已驳回的文档后端会 409；
 *   2. 不在正常入库中 —— 但**僵死必须放行**，否则文档永久卡住、只剩「预览」可用。
 *      这正是历史 bug 的成因：旧条件是 `status === 'indexing'` 就禁用，没区分僵死。
 *
 * @param row 管理员文档行
 * @returns true 表示可点击重新入库
 */
export function canReindex(
  row: Pick<AdminDocument, 'status' | 'review_status' | 'indexing_stale'>
): boolean {
  // 审核未通过：一律不可入库（后端 assert_can_index 会拒绝）
  if (row.review_status !== 'approved') {
    return false;
  }
  // 正常入库中：禁止并发重跑（避免重复计费与切片互相覆盖）
  if (row.status === 'indexing' && !row.indexing_stale) {
    return false;
  }
  return true;
}

/**
 * 「下线」按钮是否可点。
 *
 * 与重新入库同理：僵死文档必须能下线，否则「重新入库」和「下线」两种处置
 * 会被同一个状态同时堵死，管理员彻底无路可走。
 *
 * @param row 管理员文档行
 * @returns true 表示可点击下线
 */
export function canUnpublish(
  row: Pick<AdminDocument, 'status' | 'indexing_stale'>
): boolean {
  return row.status !== 'indexing' || row.indexing_stale === true;
}

/**
 * 计算「已卡在入库中多久」（分钟），用于提示文案。
 *
 * @param seconds 后端下发的 indexing_elapsed_seconds（可能为 null）
 * @returns 分钟数（至少 1，避免出现「已中断 0 分钟」这种不可读文案）；无值时返回 null
 */
export function stuckMinutes(seconds: number | null | undefined): number | null {
  if (typeof seconds !== 'number' || Number.isNaN(seconds)) {
    return null;
  }
  return Math.max(Math.round(seconds / 60), 1);
}
