import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, message } from 'antd';
import TopBar from '../components/TopBar';
import MessageList from '../components/MessageList';
import ChatInput from '../components/ChatInput';
import UploadPanel from '../components/UploadPanel';
import ConversationSidebar from '../components/ConversationSidebar';
import { useChatStream } from '../hooks/useChatStream';
import {
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
} from '../services/conversationApi';
import type { ChatMessage, ConversationSummary } from '../types';
import { useAuth } from '../hooks/useAuth';

// 对话页面：组合「左侧会话列表 / 顶部标题栏 / 中央消息区 / 底部输入框」四块，
// 整体采用豆包式居中窄栏布局。
//
// P1-F1：完成四区骨架（标题栏 / 消息区 / 气泡 / 输入框）。
// P1-F2：接入 useChatStream —— 用 EventSource 消费 /chat 流，
//        实现打字机逐字渲染、加载/停顿态（思考中、停止按钮）与错误提示。
// P1-F3：顶部新增「上传文档」入口，点击展开右侧上传抽屉（拖拽上传 + 进度 + 待审核标签）。
// P2-F1：新增左侧会话列表（新建 / 选中切换 / 删除二次确认），切换会话续聊保持上下文。
export default function ChatPage() {
  // P2-F1：会话列表与当前选中会话ID（null 表示尚未选择/新建）
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(false);

  // P1-F2：接入流式钩子；传入当前会话 ID 与「后端回传新建会话 ID」回调（P2-F1）
  const { messages, status, error, streamingId, sendMessage, stop, loadMessages } =
    useChatStream({
      conversationId: activeId,
      onConversationCreated: (id) => {
        setActiveId(id);
        void refreshConversations();
      },
    });

// P2-F2：当前登录用户与登出方法（顶栏展示用户名、提供退出登录入口）
const { user, logout } = useAuth();
// P3-F1：路由跳转（顶栏「管理后台」入口跳 /admin；仅管理员可见该入口）
const navigate = useNavigate();

// P1-F3：上传抽屉的展开状态（由顶部按钮开启，抽屉内关闭）
const [uploadOpen, setUploadOpen] = useState(false);

  // 拉取会话列表（P2-F1）
  const refreshConversations = useCallback(async () => {
    setListLoading(true);
    try {
      const list = await listConversations();
      setConversations(list);
    } catch (err) {
      // 列表加载失败不阻断对话；仅日志，避免噪音
      console.error('加载会话列表失败：', err);
    } finally {
      setListLoading(false);
    }
  }, []);

  // 首次进入：拉取历史会话列表
  useEffect(() => {
    void refreshConversations();
  }, [refreshConversations]);

  // P2-F1：新建会话 —— 直接调用后端创建空会话并选中，清空当前消息区
  const handleCreate = useCallback(async () => {
    try {
      const conv = await createConversation();
      setActiveId(conv.id);
      loadMessages([]); // 清空消息区，准备新对话
      setConversations((prev) => [conv, ...prev]);
    } catch (err) {
      message.error(`新建会话失败：${(err as Error).message}`);
    }
  }, [loadMessages]);

  // P2-F1：选中会话 —— 拉取历史消息并装载到视图（续聊保持上下文）
  const handleSelect = useCallback(
    async (id: string) => {
      if (id === activeId) return; // 已选中则跳过
      try {
        const detail = await getConversation(id);
        setActiveId(id);
        // 把后端历史映射为前端 ChatMessage（createdAt 由 ISO 时间解析为毫秒时间戳）。
        // P4-F2：一并带上 citations —— 历史回答同样要能回看引用来源，
        //        否则「刷新后引用卡片消失」会让人以为引用只在当次会话有效。
        // P6-F2：带上 serverId（历史消息的 id 本身就是后端主键）与 feedback，
        //        使用户回到旧会话时仍能看到自己点过的赞/踩，并可继续修改。
        const history: ChatMessage[] = detail.messages.map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content,
          createdAt: Date.parse(m.created_at) || Date.now(),
          citations: m.citations ?? [],
          serverId: m.id,
          feedback: m.feedback ?? null,
        }));
        loadMessages(history);
      } catch (err) {
        message.error(`加载会话失败：${(err as Error).message}`);
      }
    },
    [activeId, loadMessages]
  );

  // P2-F1：删除会话（二次确认后由侧边栏触发）—— 调后端软删除并刷新列表
  const handleDelete = useCallback(
    async (id: string) => {
      try {
        await deleteConversation(id);
        setConversations((prev) => prev.filter((c) => c.id !== id));
        // 若删除的是当前会话，清空选中与消息区
        if (id === activeId) {
          setActiveId(null);
          loadMessages([]);
        }
      } catch (err) {
        message.error(`删除会话失败：${(err as Error).message}`);
      }
    },
    [activeId, loadMessages]
  );

  return (
    // 最外层：横向 flex，左侧栏 + 右侧对话区；浅灰背景
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'row', background: '#f7f8fa' }}>
      {/* P2-F1：左侧会话列表侧边栏 */}
      <ConversationSidebar
        conversations={conversations}
        activeId={activeId}
        loading={listLoading}
        onSelect={handleSelect}
        onCreate={handleCreate}
        onDelete={handleDelete}
      />

      {/* 右侧主区域：纵向 flex 占满剩余宽度 */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        {/* P1-F3：顶部栏传入「上传文档」回调；P2-F2：传入用户与登出回调；
            P3-F1：传入「管理后台」跳转回调（TopBar 内部仅在 role=admin 时渲染该入口）。
            注意：本页即对话页，故不传 onHomeClick —— 品牌区保持纯展示，不做无意义跳转。 */}
        <TopBar
          onUploadClick={() => setUploadOpen(true)}
          user={user}
          onLogout={logout}
          onAdminClick={() => navigate('/admin')}
        />
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

        {/* P1-F3：文档上传抽屉，独立于对话区，关闭不影响正在进行的对话流 */}
        <UploadPanel open={uploadOpen} onClose={() => setUploadOpen(false)} />
      </div>
    </div>
  );
}
