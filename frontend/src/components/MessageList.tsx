import { useEffect, useRef } from 'react';
import { Empty } from 'antd';
import type { ChatMessage } from '../types';
import MessageBubble from './MessageBubble';

// 中央消息区组件（对话页骨架的核心区域）
// 职责：作为可滚动容器渲染全部消息气泡，并在新消息到达时自动滚到底部。
export default function MessageList({
  messages,
  streamingId, // P1-F2：正在流式输出的消息 ID，用于渲染打字机光标
}: {
  messages: ChatMessage[];
  streamingId: string | null;
}) {
  // 底部锚点 ref：用于滚动定位
  const endRef = useRef<HTMLDivElement>(null);

  // 副作用：messages 变化时，将视图平滑滚动到底部，保证始终看到最新消息
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // 空状态：首次进入无消息时给出引导语
  if (messages.length === 0) {
    return (
      <div
        style={{
          flex: 1,
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
        }}
      >
        <Empty description="开始和知识库助手对话吧" />
      </div>
    );
  }

  // 正常渲染：遍历消息列表逐个渲染气泡
  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '16px 20px' }}>
      {messages.map((m) => (
        <MessageBubble key={m.id} message={m} streaming={m.id === streamingId} />
      ))}
      {/* 滚动锚点：始终位于消息列表末尾 */}
      <div ref={endRef} />
    </div>
  );
}
