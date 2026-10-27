import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChatMessage, StreamStatus } from '../types';
import { startChatStream } from '../services/chatStream';

// P1-F2：对话流式状态钩子
// 职责：管理消息列表、流式状态、错误；把 SSE 收到的分片放进「打字机队列」，
//      再按固定节奏逐字渲染到助手消息上，实现打字机效果。
//
// P2-F1 增强：
//   - 支持 conversationId：发送时把当前会话 ID 带上，使后端按会话持久化与注入历史；
//   - 支持 onConversationCreated：后端在「首次发送自动建会话」时回传新会话 ID，
//     钩子通过回调通知父组件绑定到左侧列表；
//   - 支持 loadMessages / reset：切换会话时把后端返回的历史直接装载进视图（续聊保持上下文），
//     或清空消息开新会话。

// 打字机参数：每个节拍渲染的字符数与节拍间隔（毫秒）
const CHARS_PER_TICK = 2;
const TICK_MS = 24;

export interface UseChatStreamOptions {
  // 当前会话 ID；为 null 时后端自动新建会话（首次发送）。切换会话时父组件更新此值。
  conversationId: string | null;
  // 后端回传新会话 ID 时回调（首次自动建会话场景），父组件据此更新 conversationId。
  onConversationCreated?: (id: string) => void;
}

export function useChatStream(options: UseChatStreamOptions) {
  // 把最新的 options 存入 ref，避免闭包过期；同时避免 useCallback 因 options 变化频繁重建。
  const conversationIdRef = useRef<string | null>(options.conversationId);
  const onConversationCreatedRef = useRef<((id: string) => void) | undefined>(
    options.onConversationCreated
  );
  conversationIdRef.current = options.conversationId;
  onConversationCreatedRef.current = options.onConversationCreated;

  // 消息列表：当前会话的全部消息
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // 流式状态：空闲 / 思考中(等首包) / 流式输出中 / 错误
  const [status, setStatus] = useState<StreamStatus>('idle');
  // 错误提示文案
  const [error, setError] = useState<string | null>(null);
  // 正在流式输出的消息 ID（用于渲染闪烁光标）
  const [streamingId, setStreamingId] = useState<string | null>(null);

  // SSE 关闭函数引用
  const closeRef = useRef<(() => void) | null>(null);
  // 打字机定时器引用
  const timerRef = useRef<number | null>(null);
  // 待渲染字符队列（打字机缓冲区）
  const queueRef = useRef<string>('');
  // 当前打字机目标消息 ID
  const targetRef = useRef<string | null>(null);
  // 流是否已结束（用于队列排空后收尾）
  const doneRef = useRef(false);

  // 生成消息唯一 ID：时间戳 + 随机串（后续可替换为 uuid）
  const genId = () => `${Date.now()}-${Math.random().toString(36).slice(2)}`;

  // 停止打字机定时器
  const stopTyping = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  /**
   * 启动打字机：按固定节拍从队列取字符，追加到目标消息内容末尾。
   * 这样即使后端一次性下发较大分片，前端依然是逐字呈现。
   */
  const startTyping = useCallback(
    (targetId: string) => {
      targetRef.current = targetId;
      // 已在运行则不重复启动
      if (timerRef.current !== null) return;

      timerRef.current = window.setInterval(() => {
        const queue = queueRef.current;

        // 队列为空：若流已结束则收尾，否则继续等待下一批分片
        if (!queue) {
          if (doneRef.current) {
            stopTyping();
            setStatus('idle');
            setStreamingId(null);
          }
          return;
        }

        // 取出本节拍要渲染的字符，剩余部分留在队列
        const take = queue.slice(0, CHARS_PER_TICK);
        queueRef.current = queue.slice(CHARS_PER_TICK);

        const id = targetRef.current;
        setMessages((prev) =>
          prev.map((m) => (m.id === id ? { ...m, content: m.content + take } : m))
        );
      }, TICK_MS);
    },
    [stopTyping]
  );

  // 收到流分片：入队并切到「流式输出中」状态
  const enqueue = useCallback((text: string) => {
    queueRef.current += text;
    setStatus('streaming');
  }, []);

  // 中断本次流式生成：关闭 SSE + 停止打字机
  const stop = useCallback(() => {
    closeRef.current?.();
    closeRef.current = null;
    doneRef.current = true;
    stopTyping();
    queueRef.current = '';
    setStatus('idle');
    setStreamingId(null);
  }, [stopTyping]);

  /**
   * 发送提问：追加用户消息与空的助手占位消息，建立 SSE 流式连接。
   * 流式过程中禁止重复发送，避免串流。
   */
  const sendMessage = useCallback(
    (text: string) => {
      const content = text.trim();
      if (!content) return;
      if (status === 'thinking' || status === 'streaming') return; // 生成中忽略
      setError(null);

      // 用户消息
      const userMsg: ChatMessage = {
        id: genId(),
        role: 'user',
        content,
        createdAt: Date.now(),
      };
      // 助手占位消息：内容由流式分片逐步填充
      const assistantMsg: ChatMessage = {
        id: genId(),
        role: 'assistant',
        content: '',
        createdAt: Date.now(),
      };

      setMessages((prev) => [...prev, userMsg, assistantMsg]);

      // 重置本轮流状态
      doneRef.current = false;
      queueRef.current = '';
      setStatus('thinking'); // 等待首包的加载态
      setStreamingId(assistantMsg.id);
      startTyping(assistantMsg.id);

      // 建立 SSE 连接：携带当前会话 ID（为 null 时后端自动建会话并回绑）
      closeRef.current = startChatStream(content, conversationIdRef.current, {
        // 收到分片：入队等待打字机渲染
        onToken: (token) => enqueue(token),
        // 正常结束：标记结束，队列排空后打字机自动收尾
        onDone: () => {
          doneRef.current = true;
          closeRef.current = null;
        },
        // 异常：停止打字机并提示错误
        onError: (err) => {
          doneRef.current = true;
          stopTyping();
          queueRef.current = '';
          setStatus('error');
          setError(err.message);
          setStreamingId(null);
          closeRef.current = null;
        },
        // P2-F1：后端回传新建会话 ID，通知父组件绑定到左侧列表
        onConversation: (id) => {
          onConversationCreatedRef.current?.(id);
        },
      });
    },
    [status, enqueue, startTyping, stopTyping]
  );

  /**
   * 装载历史消息（P2-F1：切换会话续聊时由父组件调用）。
   * 直接把后端返回的历史消息替换当前视图，并复位流式状态（历史无需打字机动画）。
   */
  const loadMessages = useCallback(
    (msgs: ChatMessage[]) => {
      // 若正在流式，先中断，避免历史与进行中的流互相干扰
      closeRef.current?.();
      closeRef.current = null;
      doneRef.current = true;
      stopTyping();
      queueRef.current = '';
      setStreamingId(null);
      setStatus('idle');
      setError(null);
      setMessages(msgs);
    },
    [stopTyping]
  );

  // 组件卸载时清理 SSE 与定时器，防止泄漏
  useEffect(() => {
    return () => {
      closeRef.current?.();
      stopTyping();
    };
  }, [stopTyping]);

  return { messages, status, error, streamingId, sendMessage, stop, loadMessages };
}
