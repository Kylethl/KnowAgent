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
}

/**
 * 启动一次 SSE 对话流
 * @param message 用户输入的提问内容
 * @param conversationId 会话 ID（P2 接入历史后启用；当前单会话传 null）
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
