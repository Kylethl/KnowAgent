import { requestJson } from './http';
import type { ChunkDetail, IndexResult, MyDocumentList } from '../types';

// P3-F2：文档入库（RAG 管线）相关接口
// P4-F2：新增引用片段详情接口（答案引用卡片「可跳转」的数据源）
// ---------------------------------------------------------------------
// 职责：封装三类接口，供上传面板（我的文档）、管理员后台（重新入库 / 下线）
// 与对话页（查看引用原文）调用。
//
//   1) 我的文档：GET /documents
//      - 需要用户登录（未登录 401，由 http 全局处理器跳登录页）。
//      - 只返回当前登录用户提交的文档，天然隔离他人内容。
//
//   2) 重新入库 / 下线：POST /admin/documents/{id}/index、/unpublish
//      - 后端由 require_admin 保护（非 admin 返回 403），前端不做角色裁决。
//      - 令牌由 http.requestJson 自动注入，错误统一由页面层 message.error 展示后端 detail。
//
//   3) 引用原文片段：GET /documents/{doc_id}/chunks/{chunk_id}
//      - 需要登录（知识库共享，但不对匿名开放）；仅对已入库文档可读。
//      - 前端点击引用卡片时调用，在抽屉里展示该段完整正文与相邻片段。

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

/**
 * 获取引用片段的完整正文与相邻片段（P4-F2「可跳转」）。
 *
 * 说明：路径参数都用 encodeURIComponent 包裹——ID 虽是 UUID，
 * 但前端不应假设后端永远是 UUID（例如未来换成短链 ID），避免拼出坏 URL。
 *
 * @param documentId 文档 ID
 * @param chunkId 切片 ID
 */
export async function getChunkDetail(
  documentId: string,
  chunkId: string
): Promise<ChunkDetail> {
  const doc = encodeURIComponent(documentId);
  const chunk = encodeURIComponent(chunkId);
  return requestJson<ChunkDetail>(`/documents/${doc}/chunks/${chunk}`);
}
