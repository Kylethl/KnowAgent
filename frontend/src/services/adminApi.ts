import { API_PREFIX, getToken, requestJson } from './http';
import type { AdminDocumentList, DocumentPreview, ReviewPayload, ReviewResultData, ReviewStatus } from '../types';

// P3-F1：管理员审核接口
// ---------------------------------------------------------------------
// 职责：封装 /admin/* 三个接口，供管理员后台页（AdminPage）调用。
//
// 鉴权约定：
//   三个接口在后端均由 require_admin 保护（非 admin 返回 403），
//   因此本文件**不做**角色判断——角色由后端裁决，前端只负责按结果渲染。
//   令牌由 http.requestJson 自动注入（Authorization: Bearer），401 由全局处理器跳转登录页。
//
// 错误处理：
//   统一让 ApiError 上抛，由页面层 message.error 展示后端 detail 文案
//   （如「驳回时必须填写驳回理由」「该文档已审核通过，不能重复审核或驳回」）。

/**
 * 查询审核队列。
 * @param params.reviewStatus 审核结论筛选（pending / approved / rejected），不传返回全部
 * @param params.keyword 文件名关键字（后端大小写不敏感模糊匹配）
 * @param params.limit 每页条数（1~100）
 * @param params.offset 偏移量
 */
export async function listAdminDocuments(params: {
  reviewStatus?: ReviewStatus;
  keyword?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<AdminDocumentList> {
  // 手工拼 query：只在有值时拼入，避免把 undefined 传成字符串 "undefined"
  const query = new URLSearchParams();
  if (params.reviewStatus) query.set('review_status', params.reviewStatus);
  if (params.keyword) query.set('keyword', params.keyword);
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.offset !== undefined) query.set('offset', String(params.offset));

  const qs = query.toString();
  return requestJson<AdminDocumentList>(`/admin/documents${qs ? `?${qs}` : ''}`);
}

/**
 * 执行审核动作（通过 / 驳回）。
 * 驳回时必须在 note 中填写理由，后端会二次校验（不依赖前端校验）。
 * @param documentId 文档 ID
 * @param payload 审核动作与备注
 */
export async function reviewDocument(
  documentId: string,
  payload: ReviewPayload,
): Promise<ReviewResultData> {
  return requestJson<ReviewResultData>(`/admin/documents/${documentId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/**
 * 获取文档的文本预览内容（管理员判断文档质量用）。
 * @param documentId 文档 ID
 */
export async function previewDocument(documentId: string): Promise<DocumentPreview> {
  return requestJson<DocumentPreview>(`/admin/documents/${documentId}/preview`);
}

/**
 * 构造媒体原文件的访问地址（P5：图片/音频/视频预览）。
 *
 * 为什么返回 URL 而不是用 requestJson 取二进制：
 *   浏览器要靠 `<img src>` / `<audio src>` / `<video src>` 才能直接展示和播放，
 *   这些标签**无法携带 Authorization 头**，因此只能把令牌放进 query 参数
 *   （与 /chat 的 SSE 传令牌同理，后端由 require_admin_media 兼容）。
 *   同时浏览器会自行处理流式加载与播放，避免把 200MB 视频读进 JS 内存。
 *
 * @param documentId 文档 ID
 * @returns 可直接用于媒体标签的完整地址（含 ?token=）
 */
export function adminDocumentMediaUrl(documentId: string): string {
  const token = getToken() ?? '';
  return `${API_PREFIX}/admin/documents/${documentId}/media?token=${encodeURIComponent(token)}`;
}
