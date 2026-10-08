import { Alert } from 'antd';
import TopBar from '../components/TopBar';
import MessageList from '../components/MessageList';
import ChatInput from '../components/ChatInput';
import { useChatStream } from '../hooks/useChatStream';

// 对话页面：组合「顶部标题栏 / 中央消息区 / 底部输入框」三块，
// 整体采用豆包式居中窄栏布局。
//
// P1-F1：完成四区骨架（标题栏 / 消息区 / 气泡 / 输入框）。
// P1-F2：接入 useChatStream —— 用 EventSource 消费 /chat 流，
//        实现打字机逐字渲染、加载/停顿态（思考中、停止按钮）与错误提示。
export default function ChatPage() {
  // P1-F2：接入流式钩子，获取消息列表、流式状态、错误与发送/停止方法
  const { messages, status, error, streamingId, sendMessage, stop } =
    useChatStream();

  return (
    // 最外层：纵向 flex 占满视口，浅灰背景
    <div
      style={{
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        background: '#f7f8fa',
      }}
    >
      <TopBar />
      {/* 中间区域：横向居中，限制内容最大宽度，呈现豆包式窄栏对话 */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          justifyContent: 'center',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: '100%',
            maxWidth: 880, // 居中窄栏，避免长文本过宽
            display: 'flex',
            flexDirection: 'column',
            background: '#f7f8fa',
          }}
        >
          {/* 错误提示：SSE 中断或后端异常时展示，可手动关闭（P1-F2） */}
          {error && (
            <div style={{ padding: '0 20px 8px' }}>
              <Alert
                type="error"
                showIcon
                closable
                message={`对话失败：${error}`}
              />
            </div>
          )}
          <MessageList messages={messages} streamingId={streamingId} />
          <ChatInput onSend={sendMessage} onStop={stop} status={status} />
        </div>
      </div>
    </div>
  );
}
