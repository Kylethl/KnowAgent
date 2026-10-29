import { useState, type KeyboardEvent } from 'react';
import { Input, Button } from 'antd';
import { SendOutlined, PauseCircleOutlined } from '@ant-design/icons';
import type { StreamStatus } from '../types';

// 对话输入框组件
// ---------------------------------------------------------------------
// 视觉参考网页版 DeepSeek：输入区是一个独立的圆角卡片（而非贴边的直角白条），
// 内部「无边框多行文本 + 圆形发送按钮」，聚焦时描边高亮并轻微抬升阴影。
//
// 两种形态（variant）：
//   - 'hero'：无对话时使用，字号与内边距略大，作为首页视觉主体；
//   - 'dock'：有对话时使用，停靠在消息区底部。
//
// 交互约定：
//  - 多行文本，自动撑高（1~8 行）
//  - Enter 发送，Shift+Enter 换行
//  - 生成中（thinking/streaming）禁用发送，避免串流；流式中显示「停止」
export default function ChatInput({
  onSend,
  onStop,
  status,
  variant = 'dock',
}: {
  onSend: (text: string) => void; // 发送回调，透传用户输入文本
  onStop: () => void; // 停止生成回调：流式输出中可中断
  status: StreamStatus; // 流式状态：决定按钮的加载态与停止态
  variant?: 'hero' | 'dock'; // 展示形态（首页居中 / 底部停靠）
}) {
  // 受控输入框内容
  const [value, setValue] = useState('');
  // 聚焦态：用于切换描边与阴影（CSS 无法直接给父容器加 focus-within 之外的过渡）
  const [focused, setFocused] = useState(false);

  // 是否处于生成中（等待首包或流式输出中）：此时禁止再次发送，避免串流
  const busy = status === 'thinking' || status === 'streaming';
  // 首页形态：整体略微放大，承担首屏视觉重心
  const isHero = variant === 'hero';
  // 无内容时发送按钮置灰，避免误点（生成中由 loading 态接管）
  const canSend = value.trim().length > 0 && !busy;

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
    <div style={{ width: '100%' }}>
      {/* 圆角卡片容器：聚焦/悬停时描边变主色并加深阴影 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-end',
          gap: 8,
          padding: isHero ? '12px 12px 12px 20px' : '10px 10px 10px 18px',
          background: '#fff',
          // 圆角：由直角改为大圆角，形成「输入卡片」的观感
          borderRadius: isHero ? 26 : 24,
          border: `1px solid ${focused ? '#386BF3' : '#e3e5e9'}`,
          boxShadow: focused
            ? '0 6px 22px rgba(56, 107, 243, 0.14)'
            : '0 2px 10px rgba(17, 24, 39, 0.05)',
          transition: 'border-color .2s ease, box-shadow .2s ease',
        }}
      >
        {/* 无边框多行输入：由外层卡片承担边框与圆角，视觉更整体 */}
        <Input.TextArea
          variant="borderless"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder={
            isHero
              ? '向知识库提问，例如：公司的差旅报销标准是什么？'
              : '输入问题，Enter 发送，Shift+Enter 换行'
          }
          autoSize={{ minRows: 1, maxRows: 8 }}
          style={{
            flex: 1,
            // 字号与行高加大：原 14px 偏小，17/15px 更接近 DeepSeek 的阅读密度
            fontSize: isHero ? 16 : 15,
            lineHeight: 1.7,
            // 抵消 antd 默认内边距，让光标与卡片留白对齐
            padding: isHero ? '4px 0' : '6px 0',
            background: 'transparent',
            boxShadow: 'none',
            resize: 'none',
          }}
        />

        {/* 流式输出中：圆形「停止」按钮；否则圆形「发送」按钮（无内容时禁用） */}
        {status === 'streaming' ? (
          <Button
            shape="circle"
            size="large"
            icon={<PauseCircleOutlined />}
            onClick={onStop}
            title="停止生成"
          />
        ) : (
          <Button
            type="primary"
            shape="circle"
            size="large"
            icon={<SendOutlined />}
            onClick={handleSend}
            loading={status === 'thinking'}
            disabled={!canSend && status !== 'thinking'}
            title="发送（Enter）"
          />
        )}
      </div>

      {/* 快捷键提示：弱化字号与颜色，只做辅助说明 */}
      <div
        style={{
          marginTop: 8,
          textAlign: 'center',
          fontSize: 12,
          color: '#b3b7bd',
          userSelect: 'none',
        }}
      >
        Enter 发送 · Shift + Enter 换行
      </div>
    </div>
  );
}
