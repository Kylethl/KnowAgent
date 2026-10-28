import type { ChatMessage, Citation, RetrievalInfo } from '../types';
import { getToken } from './http';

// P1-F2：SSE 流式对话服务
// 职责：用 EventSource 消费后端 /chat 流，把收到的文本分片回调给上层。
// 说明：EventSource 原生只支持 GET，因此消息通过查询参数传递，
//      与后端 P1-B1「GET/POST /chat」中的 GET 分支对应。
//
// P2-F2：EventSource **无法自定义请求头**，无法携带 Authorization，
//      因此令牌只能作为 query 参数 ?token=xxx 传递（后端 get_current_user_id_sse
//      已兼容：优先取 Authorization 头，缺失时回退 query 的 token）。
//      该方案的安全权衡详见后端 core/security.py 的注释（依赖 HTTPS + 短时效令牌）。
//
// P4-F1：新增 retrieval 帧解析 —— 后端在正文之前下发
//      {"type":"retrieval","mode":"rag","count":3,...}，表示本轮回答的知识库检索状态；
//      前端据此在助手气泡下方渲染「命中 N 段资料 / 未命中 / 未启用」。
//
// P4-F2：新增 citations 帧解析 —— 后端在正文之后、[DONE] 之前下发
//      {"type":"citations","items":[{index,doc_id,doc_title,chunk_id,snippet,...}]}，
//      前端据此在回答下方渲染引用卡片（标题 / 片段 / 可点击查看原文）。
//
// P6-F2：新增 message 帧解析 —— 后端在落库之后、[DONE] 之前下发
//      {"type":"message","id":"<助手消息ID>","user_message_id":"<用户消息ID>"}。
//      流式气泡用的是前端临时 ID，而反馈接口需要数据库主键，
//      没有这一帧就无法提交点赞/点踩（反馈按钮会一直不出现）。

// 后端约定的流结束哨兵（收到即认为本轮回答结束）
export const DONE_SIGNAL = '[DONE]';

// SSE 接口地址：默认 /api/chat（nginx 反代 /api → FastAPI 的 /chat）
// 如需指向其它地址，可通过 .env 配置 VITE_CHAT_API 覆盖
const CHAT_ENDPOINT: string = import.meta.env.VITE_CHAT_API ?? '/api/chat';

// 流式事件回调集合
export interface ChatStreamHandlers {
  onToken: (token: string) => void; // 收到一个文本分片
  onDone: () => void; // 流正常结束（收到 [DONE] 或服务端关闭）
  onError: (err: Error) => void; // 流异常中断
  // P2-F1：服务端在流起始下发 {type:"conversation",id} 帧，
  //       用于「首次发送自动建会话」时把新会话 ID 回绑给前端（绑定左侧列表）。
  onConversation?: (id: string) => void;
  // P4-F1：服务端下发 {type:"retrieval",...} 帧，回传本轮知识库检索状态
  onRetrieval?: (info: RetrievalInfo) => void;
  // P4-F2：服务端下发 {type:"citations",...} 帧，回传本轮引用来源
  onCitations?: (items: Citation[]) => void;
  // P6-F2：服务端在落库后下发 {type:"message",id} 帧，回传**真实消息 ID**。
  //       前端据此把临时气泡 ID 换成数据库主键，点赞/点踩才有提交目标。
  onMessage?: (info: { id: string; userMessageId?: string }) => void;
}

/**
 * 启动一次 SSE 对话流
 * @param message 用户输入的提问内容
 * @param conversationId 会话 ID（P2 启用；不传则后端自动新建会话并回绑 ID）
 * @param handlers 流事件回调
 * @returns 关闭函数，调用即中断流（用于「停止生成」）
 */
export function startChatStream(
  message: string,
  conversationId: string | null,
  handlers: ChatStreamHandlers
): () => void {
  // 组装 GET 请求地址：EventSource 不支持请求体，参数走 query
  const url = new URL(CHAT_ENDPOINT, window.location.origin);
  url.searchParams.set('message', message);
  if (conversationId) {
    url.searchParams.set('conversation_id', conversationId);
  }
  // P2-F2：附带 JWT。EventSource 不能设置请求头，故令牌走 query 参数。
  // 后端 GET /chat 的鉴权依赖会读取该参数；无令牌时（未登录）服务端返回 401，
  // 浏览器对 SSE 的 401 不会走 onerror 的文本解析，此时表现为连接被拒。
  const token = getToken();
  if (token) {
    url.searchParams.set('token', token);
  }

  // 建立 SSE 长连接
  const es = new EventSource(url.toString());

  // 收到服务端推送的数据帧
  es.onmessage = (event: MessageEvent<string>) => {
    const raw = event.data;

    // 结束哨兵：关闭连接并回调完成
    if (raw === DONE_SIGNAL) {
      handlers.onDone();
      es.close();
      return;
    }

    // P2-F1：会话回绑帧 —— {"type":"conversation","id":"..."}
    // 仅解析一次（同一流里 id 不变），交由上层把新会话绑定到左侧列表。
    const bound = tryParseConversation(raw);
    if (bound) {
      handlers.onConversation?.(bound);
      return;
    }

    // P4-F1：检索状态帧 —— {"type":"retrieval","mode":"rag","count":3,...}
    // 该帧在正文分片之前到达，只解析一次，交由上层挂到本轮助手消息上。
    const retrieval = tryParseRetrieval(raw);
    if (retrieval) {
      handlers.onRetrieval?.(retrieval);
      return;
    }

    // P4-F2：引用来源帧 —— {"type":"citations","items":[...]}
    // 该帧在正文之后、[DONE] 之前到达；解析为空数组时也视为有效帧，
    // 避免空 JSON 被当作正文拼进回答。
    const citations = tryParseCitations(raw);
    if (citations) {
      handlers.onCitations?.(citations);
      return;
    }

    // P6-F2：消息回执帧 —— {"type":"message","id":"uuid","user_message_id":"uuid"}
    // 在落库之后、[DONE] 之前到达；拿到它之后才允许显示反馈按钮。
    const messageId = tryParseMessage(raw);
    if (messageId) {
      handlers.onMessage?.(messageId);
      return;
    }

    // 兼容两种数据格式：JSON（{"content":"..."}）与纯文本分片
    const text = extractContent(raw);
    if (text) {
      handlers.onToken(text);
    }
  };

  // 连接异常或中断：回调错误并关闭，避免浏览器自动重连
  es.onerror = () => {
    handlers.onError(new Error('与服务器的连接中断，请稍后重试'));
    es.close();
  };

  // 返回中断函数
  return () => es.close();
}

/**
 * 尝试从 SSE data 帧中解析会话回绑信息。
 * 形如 {"type":"conversation","id":"uuid"} 时返回 id，否则返回 null。
 * 解析失败（非 JSON / 非该类型）一律返回 null，避免影响正文分片处理。
 */
function tryParseConversation(raw: string): string | null {
  if (!raw.startsWith('{')) return null;
  try {
    const parsed = JSON.parse(raw) as { type?: unknown; id?: unknown };
    if (parsed.type === 'conversation' && typeof parsed.id === 'string') {
      return parsed.id;
    }
  } catch {
    // 非该类型帧，交由 extractContent 处理
  }
  return null;
}

/**
 * 尝试从 SSE data 帧中解析「知识库检索状态」。
 * 形如 {"type":"retrieval","mode":"rag","count":3} 时返回规范化对象，否则返回 null。
 *
 * 容错约定（后端字段可能因版本演进增减，前端不应因此崩溃）：
 * - mode 不在四种已知取值内 → 视为 undefined 的下发，直接忽略该帧；
 * - count 缺失或非法 → 退化为 0（「已检索但未命中」的展示仍然正确）。
 */
function tryParseRetrieval(raw: string): RetrievalInfo | null {
  if (!raw.startsWith('{')) return null;
  try {
    const parsed = JSON.parse(raw) as { type?: unknown; [key: string]: unknown };
    if (parsed.type !== 'retrieval') return null;

    // 只接受后端约定的四种 mode，防止脏数据让界面出现未知状态
    const mode = parsed.mode;
    if (mode !== 'rag' && mode !== 'no_hit' && mode !== 'disabled' && mode !== 'fallback') {
      return null;
    }

    return {
      mode,
      // count 非法时按 0 处理（Number 转换失败会得到 NaN，需显式兜底）
      count: typeof parsed.count === 'number' && Number.isFinite(parsed.count) ? parsed.count : 0,
      cached: typeof parsed.cached === 'boolean' ? parsed.cached : undefined,
      elapsed_ms: typeof parsed.elapsed_ms === 'number' ? parsed.elapsed_ms : undefined,
      reason: typeof parsed.reason === 'string' ? parsed.reason : undefined,
    };
  } catch {
    // 非该类型帧，交由 extractContent 处理
  }
  return null;
}

/**
 * 尝试从 SSE data 帧中解析「答案引用来源」。
 * 形如 {"type":"citations","items":[Citation,...]} 时返回规范化列表，否则返回 null。
 *
 * 返回值契约：解析成功时**一定返回数组**（可能为空），因此上层用 `if (result)`
 * 判断即可——空数组也是有效帧，不能被误当成正文。
 *
 * 逐条容错：缺少 chunk_id / doc_id 的条目无法跳转，直接丢弃；
 * snippet 缺失时退化为空串（卡片仍可展示标题与序号）。
 */
function tryParseCitations(raw: string): Citation[] | null {
  if (!raw.startsWith('{')) return null;
  try {
    const parsed = JSON.parse(raw) as { type?: unknown; items?: unknown };
    if (parsed.type !== 'citations') return null;
    if (!Array.isArray(parsed.items)) return [];

    const items: Citation[] = [];
    for (const raw of parsed.items) {
      if (!raw || typeof raw !== 'object') continue;
      const item = raw as Record<string, unknown>;
      // 跳转必需的字段：文档 ID 与切片 ID（缺任一个卡片就点不开）
      if (typeof item.doc_id !== 'string' || typeof item.chunk_id !== 'string') continue;
      items.push({
        index: typeof item.index === 'number' ? item.index : items.length + 1,
        doc_id: item.doc_id,
        doc_title: typeof item.doc_title === 'string' ? item.doc_title : '未命名文档',
        file_type: typeof item.file_type === 'string' ? item.file_type : '',
        chunk_id: item.chunk_id,
        chunk_index: typeof item.chunk_index === 'number' ? item.chunk_index : 0,
        heading: typeof item.heading === 'string' ? item.heading : '',
        snippet: typeof item.snippet === 'string' ? item.snippet : '',
        score: typeof item.score === 'number' ? item.score : 0,
        vector_score: typeof item.vector_score === 'number' ? item.vector_score : 0,
        cited: item.cited === true,
      });
    }
    return items;
  } catch {
    // 非该类型帧，交由 extractContent 处理
  }
  return null;
}

/**
 * 尝试从 SSE data 帧中解析「消息回执」（P6-F2）。
 * 形如 {"type":"message","id":"uuid","user_message_id":"uuid"} 时返回对象，否则返回 null。
 *
 * 容错：缺少 id 的帧直接忽略——没有主键的回执毫无用处，
 * 若把它当成有效帧，上层会拿到空 ID 并渲染出点了必然 404 的反馈按钮。
 */
function tryParseMessage(raw: string): { id: string; userMessageId?: string } | null {
  if (!raw.startsWith('{')) return null;
  try {
    const parsed = JSON.parse(raw) as {
      type?: unknown;
      id?: unknown;
      user_message_id?: unknown;
    };
    if (parsed.type !== 'message') return null;
    if (typeof parsed.id !== 'string' || parsed.id === '') return null;

    return {
      id: parsed.id,
      userMessageId: typeof parsed.user_message_id === 'string' ? parsed.user_message_id : undefined,
    };
  } catch {
    // 非该类型帧，交由 extractContent 处理
  }
  return null;
}

/**
 * 从 SSE data 帧中提取文本内容
 * - 若形如 JSON（以 { 开头）则取 content 字段
 * - 否则按纯文本分片处理
 * 这样可同时兼容后端的不同下发格式。
 *
 * 重要：能解析成 JSON 但没有 content 字段的帧一律返回空串。
 * 否则（旧实现）会把整段 JSON 原样当成正文拼进气泡——
 * 一旦后端新增帧类型而前端尚未适配，用户就会在回答里看到一段原始 JSON。
 */
function extractContent(raw: string): string {
  if (raw.startsWith('{')) {
    try {
      const parsed = JSON.parse(raw) as { content?: unknown };
      if (typeof parsed.content === 'string') {
        return parsed.content;
      }
      // 合法 JSON 但没有 content：说明是某种控制帧（conversation/retrieval/citations/message…），
      // 上层已各自处理过；此处返回空串，绝不能把 JSON 原文当正文
      return '';
    } catch {
      // JSON 解析失败时回退为纯文本
      return raw;
    }
  }
  return raw;
}

// 便于类型复用：导出消息类型（供上层按需引用）
export type { ChatMessage };
