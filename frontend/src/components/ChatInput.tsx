import { useState, type KeyboardEvent } from 'react';
import { Input, Button } from 'antd';
import { SendOutlined, PauseCircleOutlined } from '@ant-design/icons';
import type { StreamStatus } from '../types';

// 底部输入框组件（对话页骨架的输入区）
// 交互约定：
//  - 多行文本输入，自动撑高（1~5 行）
//  - Enter 发送，Shift+Enter 换行
//  - 生成中（thinking/streaming）禁用发送按钮，避免重复提交与串流
//  - 流式输出中显示「停止」按钮，可中断生成（P1-F2）
export default function ChatInput({
  onSend,
  onStop,
  status,
}: {
  onSend: (text: string) => void; // 发送回调，透传用户输入文本
  onStop: () => void; // 停止生成回调：流式输出中可中断
  status: StreamStatus; // 流式状态：决定按钮的加载态与停止态
}) {
  // 受控输入框内容
  const [value, setValue] = useState('');

  // 是否处于生成中（等待首包或流式输出中）：此时禁止再次发送，避免串流
  const busy = status === 'thinking' || status === 'streaming';

  // 执行发送：去除首尾空白后若非空且非生成中，则回调并清空输入框
  const handleSend = () => {
    const text = value.trim();
    if (!text || busy) return; // 空内容或生成中直接忽略
    onSend(text);
    setValue('');
  };

  // 键盘事件处理：Enter 发送，Shift+Enter 允许换行
  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault(); // 阻止默认的换行行为
      handleSend();
    }
  };

  return (
    <div
      style={{
        borderTop: '1px solid #ececec', // 与消息区分隔
        padding: '12px 20px',
        background: '#fff',
      }}
    >
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
        {/* 多行输入框：自适应高度，回车发送 */}
        <Input.TextArea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          autoSize={{ minRows: 1, maxRows: 5 }}
          style={{ flex: 1 }}
        />
        {/* 流式输出中：显示「停止」按钮以中断生成；
            否则显示「发送」，等待首包（thinking）时按钮转加载态 */}
        {status === 'streaming' ? (
          <Button icon={<PauseCircleOutlined />} onClick={onStop}>
            停止
          </Button>
        ) : (
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={handleSend}
            loading={status === 'thinking'}
          >
            发送
          </Button>
        )}
      </div>
    </div>
  );
}
