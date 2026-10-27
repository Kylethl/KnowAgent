import { requestJson } from './http';
import type { IndexResult, MyDocumentList } from '../types';

// P3-F2：文档入库（RAG 管线）相关接口
// ---------------------------------------------------------------------
// 职责：封装两类接口，供上传面板（我的文档）与管理员后台（重新入库 / 下线）调用。
//
//   1) 我的文档：GET /documents
//      - 需要用户登录（未登录 401，由 http 全局处理器跳登录页）。
//      - 只返回当前登录用户提交的文档，天然隔离他人内容。
//
//   2) 重新入库 / 下线：POST /admin/documents/{id}/index、/unpublish
//      - 后端由 require_admin 保护（非 admin 返回 403），前端不做角色裁决。
//      - 令牌由 http.requestJson 自动注入，错误统一由页面层 message.error 展示后端 detail。

/**
 * 获取「我的文档」列表（含入库状态）。
 * @param limit 返回条数（默认 50，后端限制 1~100）
 */
export async function getMyDocuments(limit = 50): Promise<MyDocumentList> {
  const qs = new URLSearchParams({ limit: String(limit) }).toString();
  return requestJson<MyDocumentList>(`/documents?${qs}`);
}

/**
 * 触发文档重新入库（管理员）。
 * 适用于入库失败后重试、或文档内容更新后重建索引。
 * @param documentId 文档 ID
 */
export async function adminReindexDocument(documentId: string): Promise<IndexResult> {
  return requestJson<IndexResult>(`/admin/documents/${documentId}/index`, { method: 'POST' });
}

/**
 * 文档下线（管理员）。
 * 删除该文档的全部向量切片，使其不再参与检索；审核结论保持不变，可再次入库。
 * @param documentId 文档 ID
 */
export async function adminUnpublishDocument(documentId: string): Promise<IndexResult> {
  return requestJson<IndexResult>(`/admin/documents/${documentId}/unpublish`, { method: 'POST' });
}
