import { requestJson } from './http';
import type {
  AuditActionOption,
  AuditLogList,
  BackupItem,
  BackupStatus,
  ComplianceReport,
  OpsStatus,
  QuotaInfo,
} from '../types';

// P7：运维接口封装（系统状态 / 审计日志 / 备份 / 合规自检 / 配额）
// ---------------------------------------------------------------------
// 契约来源：《需求清单》P7（监控 / 日志 / 备份；合规与私有化部署）
//
// 鉴权约定：
//   /ops/* 全部由后端 require_admin 保护，普通用户会收到 403；
//   本文件不做角色判断（角色由后端裁决），页面由 <RequireAdmin> 先挡一层。
//   /chat/quota 只要求登录（按人查询配额），令牌由 requestJson 自动注入。
//
// 错误处理：
//   统一让 ApiError 上抛，由页面用 message.error 展示后端 detail
//   （如「pg_dump 不可用：…」这种可执行的原因，比「请求失败」有用得多）。

/**
 * 查询系统状态总览。
 * 后端会在一次调用里并发探测各依赖，正常耗时 < 2 秒。
 */
export async function fetchOpsStatus(): Promise<OpsStatus> {
  return requestJson<OpsStatus>('/ops/status');
}

/**
 * 查询合规自检报告。
 * 会额外检查 Redis 持久化状态，耗时略长于状态总览。
 */
export async function fetchCompliance(): Promise<ComplianceReport> {
  return requestJson<ComplianceReport>('/ops/compliance');
}

/**
 * 查询审计日志（分页 + 多条件筛选）。
 * @param params.action 动作代码精确匹配（如 document.review）
 * @param params.result success / failure
 * @param params.keyword 关键字（用户名 / 动作 / 资源 ID 模糊匹配）
 * @param params.start 起始时间（ISO 8601）
 * @param params.end 结束时间（ISO 8601）
 * @param params.limit 每页条数
 * @param params.offset 偏移量
 */
export async function fetchAuditLogs(
  params: {
    action?: string;
    result?: 'success' | 'failure';
    keyword?: string;
    start?: string;
    end?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<AuditLogList> {
  // 手工拼 query：只在有值时拼入，避免把 undefined 传成字符串 "undefined"
  const query = new URLSearchParams();
  if (params.action) query.set('action', params.action);
  if (params.result) query.set('result', params.result);
  if (params.keyword) query.set('keyword', params.keyword);
  if (params.start) query.set('start', params.start);
  if (params.end) query.set('end', params.end);
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.offset !== undefined) query.set('offset', String(params.offset));

  const qs = query.toString();
  return requestJson<AuditLogList>(`/ops/audit-logs${qs ? `?${qs}` : ''}`);
}

/**
 * 查询审计动作字典（筛选下拉的数据源）。
 * 放在后端而非前端硬编码：新增动作时只改一处，避免前后端字典漂移。
 */
export async function fetchAuditActions(): Promise<AuditActionOption[]> {
  return requestJson<AuditActionOption[]>('/ops/audit-logs/actions');
}

/**
 * 查询备份列表与总览。
 * 返回结构把「策略 + 列表」放在一起，页面一次请求即可渲染整张卡片。
 */
export async function fetchBackups(
  limit = 50,
): Promise<{ status: BackupStatus; items: BackupItem[] }> {
  return requestJson<{ status: BackupStatus; items: BackupItem[] }>(
    `/ops/backups?limit=${limit}`,
  );
}

/**
 * 立即执行一次数据库备份（管理员）。
 *
 * 注意：同步执行，大库可能耗时数十秒到数分钟；
 * 调用方应设置 loading 并提示「执行中，请勿重复点击」。
 */
export async function triggerBackup(): Promise<BackupItem> {
  return requestJson<BackupItem>('/ops/backups', { method: 'POST' });
}

/** 按保留策略清理过期备份，返回删除数量。 */
export async function pruneBackups(): Promise<{
  removed: number;
  status: BackupStatus;
}> {
  return requestJson<{ removed: number; status: BackupStatus }>('/ops/backups/prune', {
    method: 'POST',
  });
}

/**
 * 查询当前用户的对话配额（只读，不消耗额度）。
 *
 * 为什么前端要预检：
 *   对话走 EventSource，浏览器拿不到 429 的响应体与状态码，
 *   用户只会看到「连接失败」。先查配额就能给出「今日额度已用完，明日 00:00 重置」
 *   这种可读提示，而不是让人一脸茫然地重试。
 */
export async function fetchQuota(): Promise<QuotaInfo> {
  return requestJson<QuotaInfo>('/chat/quota');
}
