import { Button, Empty, List, Popconfirm, Spin, Tooltip, Typography } from 'antd';
import {
  CommentOutlined,
  DeleteOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import type { ConversationSummary } from '../types';

// 左侧会话列表侧边栏（P2-F1 核心组件）
// 职责：渲染「新建 / 选中切换 / 删除（二次确认）」三件套，并高亮当前会话。
//   设计参考豆包：左侧窄栏，顶部「新建会话」按钮，下方可滚动的会话列表，
//   每条会话显示标题 + 最近活跃时间，鼠标悬停时出现删除按钮（带二次确认）。
//
// 说明：当前为「无登录」阶段，会话归属后端 Demo 用户（P2-B2 登录后按真实用户隔离），
//       组件本身不感知用户，只负责展示与交互回调。

interface Props {
  conversations: ConversationSummary[]; // 会话列表（按最近活跃倒序）
  activeId: string | null; // 当前选中的会话 ID（高亮用）
  loading?: boolean; // 列表加载中
  onSelect: (id: string) => void; // 选中某会话（切换并续聊）
  onCreate: () => void; // 新建会话
  onDelete: (id: string) => void; // 删除会话（二次确认后触发）
}

// 把 ISO 时间格式化为「今天 HH:mm / 昨天 / MM-DD」等简短形式，便于列表展示
function formatUpdated(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (sameDay) return `今天 ${hhmm}`;
  if (isYesterday) return `昨天 ${hhmm}`;
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export default function ConversationSidebar({
  conversations,
  activeId,
  loading = false,
  onSelect,
  onCreate,
  onDelete,
}: Props) {
  return (
    <aside
      style={{
        width: 260,
        flexShrink: 0,
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        background: '#fff',
        borderRight: '1px solid #ececec',
      }}
    >
      {/* 顶部：标题 + 新建会话按钮 */}
      <div
        style={{
          padding: '12px 12px 8px',
          borderBottom: '1px solid #f0f0f0',
        }}
      >
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
          对话历史
        </Typography.Text>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          block
          onClick={onCreate}
        >
          新建会话
        </Button>
      </div>

      {/* 会话列表：可滚动；加载中显示 Spin，空列表显示引导空状态 */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '8px' }}>
        {loading ? (
          <div style={{ padding: 24, textAlign: 'center' }}>
            <Spin />
          </div>
        ) : conversations.length === 0 ? (
          <div style={{ padding: 24 }}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="还没有会话，点击「新建会话」开始"
            />
          </div>
        ) : (
          <List
            dataSource={conversations}
            renderItem={(item) => {
              const isActive = item.id === activeId;
              return (
                <List.Item
                  // 整条可点击：选中并切换到该会话（续聊保持上下文）
                  onClick={() => onSelect(item.id)}
                  style={{
                    padding: '8px 10px',
                    borderRadius: 8,
                    cursor: 'pointer',
                    marginBottom: 4,
                    background: isActive ? '#eef2ff' : 'transparent',
                    border: isActive ? '1px solid #386BF3' : '1px solid transparent',
                    transition: 'background 0.15s',
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {/* 标题：首条问题自动生成；为空时回退「新对话」 */}
                    <div
                      style={{
                        fontSize: 14,
                        color: '#1f1f1f',
                        whiteSpace: 'nowrap',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                      }}
                    >
                      <CommentOutlined style={{ marginRight: 6, color: '#8c8c8c' }} />
                      {item.title || '新对话'}
                    </div>
                    <div
                      style={{
                        fontSize: 12,
                        color: '#8c8c8c',
                        marginTop: 2,
                        paddingLeft: 24,
                      }}
                    >
                      {formatUpdated(item.updated_at)}
                      {item.message_count > 0 ? ` · ${item.message_count} 条` : ''}
                    </div>
                  </div>

                  {/* 删除按钮：悬停整条时显示；带 Popconfirm 二次确认 */}
                  <Popconfirm
                    title="删除会话"
                    description="删除后将从列表中移除，确定继续吗？"
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => onDelete(item.id)}
                    onCancel={() => {}}
                  >
                    <Tooltip title="删除会话">
                      <DeleteOutlined
                        onClick={(e) => e.stopPropagation()}
                        style={{ color: '#bfbfbf', padding: '4px', fontSize: 16 }}
                      />
                    </Tooltip>
                  </Popconfirm>
                </List.Item>
              );
            }}
          />
        )}
      </div>
    </aside>
  );
}
