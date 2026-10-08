import { Avatar } from 'antd';
import { UserOutlined, RobotOutlined } from '@ant-design/icons';
import type { ChatMessage } from '../types';

// 单条消息气泡组件
// 布局规则（参考豆包）：
//  - 用户消息：靠右，主色填充气泡，右侧用户头像
//  - 助手消息：靠左，浅灰气泡，左侧机器人头像
export default function MessageBubble({
  message,
  streaming = false, // P1-F2：该消息是否正在流式输出
}: {
  message: ChatMessage;
  streaming?: boolean;
}) {
  // 判断是否为用户消息，决定对齐方向与配色
  const isUser = message.role === 'user';

  return (
    <div
      style={{
        display: 'flex',
        justifyContent: isUser ? 'flex-end' : 'flex-start', // 用户右、助手左
        margin: '12px 0',
        gap: 8,
      }}
    >
      {/* 助手消息在左侧显示机器人头像 */}
      {!isUser && (
        <Avatar icon={<RobotOutlined />} style={{ background: '#386BF3' }} />
      )}

      {/* 气泡本体：根据角色切换背景与文字颜色 */}
      <div
        style={{
          maxWidth: '70%', // 限制最大宽度，避免长文本撑满
          padding: '10px 14px',
          borderRadius: 12,
          background: isUser ? '#386BF3' : '#f2f3f5', // 用户主色 / 助手浅灰
          color: isUser ? '#fff' : '#1f1f1f',
          whiteSpace: 'pre-wrap', // 保留换行与空格
          wordBreak: 'break-word', // 长单词自动换行
          lineHeight: 1.6,
        }}
      >
        {/* 流式输出中：内容为空显示「思考中」（加载/停顿态），
            有内容则在正文后追加闪烁光标，呈现打字机效果 */}
        {streaming && message.content === '' ? (
          <>
            思考中
            <span className="typing-cursor" />
          </>
        ) : (
          <>
            {message.content}
            {streaming && <span className="typing-cursor" />}
          </>
        )}
      </div>

      {/* 用户消息在右侧显示用户头像 */}
      {isUser && (
        <Avatar icon={<UserOutlined />} style={{ background: '#bfbfbf' }} />
      )}
    </div>
  );
}
