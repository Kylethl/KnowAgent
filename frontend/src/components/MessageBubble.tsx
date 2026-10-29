import { Avatar } from 'antd';
import { UserOutlined, RobotOutlined } from '@ant-design/icons';
import type { ChatMessage } from '../types';
import RetrievalBadge from './RetrievalBadge';
import CitationList from './CitationList';
import FeedbackButtons from './FeedbackButtons';

// 单条消息气泡组件
// 布局规则（参考豆包）：
//  - 用户消息：靠右，主色填充气泡，右侧用户头像
//  - 助手消息：靠左，浅灰气泡，左侧机器人头像
//
// P4-F1：助手消息在流式结束（或流式过程中）可携带知识库检索状态，
//        在气泡下方渲染「知识库检索 · 命中 N 段资料」等徽标。
// P4-F2：助手消息还可携带引用来源，在徽标下方渲染引用卡片（标题/片段/可跳转）。
// P6-F2：助手消息底部提供点赞/点踩入口（依赖后端回填的真实消息 ID）。
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

      {/* 助手列：气泡 + 检索状态徽标 + 引用卡片纵向排列；用户消息只有气泡 */}
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          // 用户消息靠右对齐，助手消息靠左对齐
          alignItems: isUser ? 'flex-end' : 'flex-start',
          maxWidth: '76%', // 限制最大宽度，避免长文本撑满（内容栏加宽后同步放宽）
        }}
      >
        {/* 气泡本体：根据角色切换背景与文字颜色。
            圆角：18px 大圆角，靠近头像的一侧收小（6px）形成「气泡尖角」，
            比纯直角更柔和，也比四角等圆更接近现代对话产品。 */}
        <div
          style={{
            padding: '11px 16px',
            borderRadius: isUser ? '18px 6px 18px 18px' : '6px 18px 18px 18px',
            background: isUser ? '#386BF3' : '#f2f3f5', // 用户主色 / 助手浅灰
            color: isUser ? '#fff' : '#1f1f1f',
            whiteSpace: 'pre-wrap', // 保留换行与空格
            wordBreak: 'break-word', // 长单词自动换行
            lineHeight: 1.7,
            fontSize: 15,
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

        {/* P4-F1：检索状态徽标（仅助手消息；无 retrieval 时不渲染） */}
        {!isUser && <RetrievalBadge info={message.retrieval} />}

        {/* P4-F2：引用来源卡片（仅助手消息；无 citations 时不渲染）。
            流式过程中不展示：引用帧在正文之后才到，提前占位会抖动。 */}
        {!isUser && !streaming && <CitationList citations={message.citations} />}

        {/* P6-F2：答案反馈（点赞/点踩）。同样只在流式结束后出现——
            流式过程中后端尚未落库，没有可提交反馈的消息 ID。
            serverId 由后端 message 帧回填，缺失时组件自身不渲染。 */}
        {!isUser && !streaming && (
          <FeedbackButtons messageId={message.serverId} initial={message.feedback} />
        )}
      </div>

      {/* 用户消息在右侧显示用户头像 */}
      {isUser && (
        <Avatar icon={<UserOutlined />} style={{ background: '#bfbfbf' }} />
      )}
    </div>
  );
}
