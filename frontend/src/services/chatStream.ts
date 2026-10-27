import type { ChatMessage } from '../types';

// P1-F2：SSE 流式对话服务
// 职责：用 EventSource 消费后端 /chat 流，把收到的文本分片回调给上层。
// 说明：EventSource 原生只支持 GET，因此消息通过查询参数传递，
//      与后端 P1-B1「GET/POST /chat」中的 GET 分支对应。

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
 * 从 SSE data 帧中提取文本内容
 * - 若形如 JSON（以 { 开头）则取 content 字段
 * - 否则按纯文本分片处理
 * 这样可同时兼容后端的不同下发格式。
 */
function extractContent(raw: string): string {
  if (raw.startsWith('{')) {
    try {
      const parsed = JSON.parse(raw) as { content?: unknown };
      if (typeof parsed.content === 'string') {
        return parsed.content;
      }
    } catch {
      // JSON 解析失败时回退为纯文本
      return raw;
    }
  }
  return raw;
}

// 便于类型复用：导出消息类型（供上层按需引用）
export type { ChatMessage };
