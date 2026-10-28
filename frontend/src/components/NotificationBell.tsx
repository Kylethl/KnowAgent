import { useEffect, useState } from 'react';
import { Badge, Button, Empty, Popover, Spin, Tooltip, Typography, notification } from 'antd';
import {
  BellOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  CheckOutlined,
} from '@ant-design/icons';
import type { NotificationItem } from '../types';
import { useNotifications } from '../hooks/useNotifications';

// P6-F1：顶栏通知铃铛（站内信入口）
// ---------------------------------------------------------------------
// 契约来源：《需求清单》P6 功能1「审核通知」
//   「前端：站内信 / Toast 提示「文档已通过 / 被驳回」」
//
// 本组件承担两件事：
//   1. 站内信：铃铛 + 未读角标 + 下拉面板（列表 / 单条已读 / 全部已读）；
//   2. Toast：新通知到达时弹出可点击的浮层提示，让用户不必主动点铃铛也能知道结果。
//
// 为什么放在 TopBar 里：
//   对话页与管理后台都复用 TopBar，通知是全局能力（管理员也可能提交文档），
//   挂在顶栏即可覆盖全部登录页面，不必每个页面各写一遍。
//
// 状态管理：全部来自 useNotifications（轮询、去重、标记已读），
// 本组件只负责渲染与弹窗，保持「逻辑在 hook、展示在组件」。

interface Props {
  // 是否启用轮询：未登录时不渲染本组件（由 TopBar 控制），故默认 true 即可
  enabled?: boolean;
}

/** 把 ISO 时间格式化为「刚刚 / n 分钟前 / n 小时前 / 日期」，让通知有时效感。 */
function formatRelative(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;

  const diffMs = Date.now() - t;
  const diffMin = Math.floor(diffMs / 60_000);

  if (diffMin < 1) return '刚刚';
  if (diffMin < 60) return `${diffMin} 分钟前`;
  if (diffMin < 60 * 24) return `${Math.floor(diffMin / 60)} 小时前`;

  // 超过一天：直接显示日期，比「3 天前」更好定位
  const d = new Date(t);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 按通知类型返回图标与配色：通过=绿勾，驳回=红叉。 */
function typeMeta(type: string) {
  if (type === 'document_rejected') {
    return { icon: <CloseCircleFilled style={{ color: '#ff4d4f' }} />, color: '#ff4d4f' };
  }
  return { icon: <CheckCircleFilled style={{ color: '#52c41a' }} />, color: '#52c41a' };
}

export default function NotificationBell({ enabled = true }: Props) {
  const { unread, items, loading, error, pendingToasts, refresh, markRead, markAllRead, dismissToasts } =
    useNotifications(enabled);

  // 面板展开态：打开时顺手刷新一次，保证看到的是最新状态
  const [open, setOpen] = useState(false);

  /**
   * 新通知 Toast：用 antd 的 notification 浮层（比 message 更适合承载标题+正文）。
   * 每条只弹一次——弹完立即清空队列，否则每次重渲染都会重复弹出。
   */
  useEffect(() => {
    if (pendingToasts.length === 0) return;

    pendingToasts.forEach((n) => {
      const meta = typeMeta(n.type);
      notification.open({
        message: n.title,
        description: n.content,
        icon: meta.icon,
        // 8 秒足够读完一条审核结果；点击浮层即视为已读并打开面板
        duration: 8,
        placement: 'topRight',
        onClick: () => {
          void markRead(n.id);
          setOpen(true);
        },
        style: { cursor: 'pointer' },
      });
    });

    // 弹完清空待弹队列
    dismissToasts();
  }, [pendingToasts, dismissToasts, markRead]);

  /** 展开面板：同时刷新列表（用户主动查看时一定要最新）。 */
  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    if (next) void refresh();
  };

  // 面板内容：标题栏 + 通知列表（或空态/加载态/错误提示）
  const panel = (
    <div style={{ width: 340 }}>
      {/* 面板头部：标题 + 全部已读 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          paddingBottom: 8,
          borderBottom: '1px solid #f0f0f0',
        }}
      >
        <Typography.Text strong>通知</Typography.Text>
        <Button
          type="link"
          size="small"
          icon={<CheckOutlined />}
          // 没有未读时禁用，避免点了一个什么都不做的按钮
          disabled={unread === 0}
          onClick={() => void markAllRead()}
        >
          全部已读
        </Button>
      </div>

      {/* 内容区：固定最大高度 + 滚动，避免通知多时把页面撑长 */}
      <div style={{ maxHeight: 360, overflowY: 'auto', marginTop: 4 }}>
        {loading && items.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '24px 0' }}>
            <Spin size="small" />
          </div>
        ) : error && items.length === 0 ? (
          <div style={{ padding: '16px 4px', color: '#ff4d4f', fontSize: 13 }}>{error}</div>
        ) : items.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="暂无通知"
            style={{ padding: '16px 0' }}
          />
        ) : (
          items.map((n) => (
            <NotificationRow key={n.id} item={n} onRead={() => void markRead(n.id)} />
          ))
        )}
      </div>
    </div>
  );

  return (
    <Popover
      content={panel}
      trigger="click"
      placement="bottomRight"
      open={open}
      onOpenChange={handleOpenChange}
      // 面板内有点击操作（标记已读），用 click 触发更稳定
      destroyTooltipOnHide={false}
    >
      <Tooltip title="通知">
        <Badge count={unread} size="small" offset={[-2, 2]}>
          <Button
            type="text"
            icon={<BellOutlined style={{ fontSize: 18 }} />}
            // 无障碍：屏幕阅读器能读出「通知，N 条未读」
            aria-label={unread > 0 ? `通知，${unread} 条未读` : '通知'}
          />
        </Badge>
      </Tooltip>
    </Popover>
  );
}

/**
 * 通知列表中的一行：类型图标 + 标题 + 正文摘要 + 相对时间 + 未读圆点。
 * 点击即标记已读（不跳转：用户侧没有「文档详情页」，跳转反而会丢失上下文）。
 */
function NotificationRow({ item, onRead }: { item: NotificationItem; onRead: () => void }) {
  const meta = typeMeta(item.type);
  const unread = !item.read_at;

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onRead}
      onKeyDown={(e) => {
        // 键盘可达：回车/空格等同于点击
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onRead();
        }
      }}
      style={{
        display: 'flex',
        gap: 10,
        padding: '10px 8px',
        borderRadius: 8,
        cursor: unread ? 'pointer' : 'default',
        // 未读用极浅底色区分，避免整行加粗造成视觉噪音
        background: unread ? '#f7f9ff' : 'transparent',
        outline: 'none',
      }}
    >
      {/* 类型图标：通过=绿勾 / 驳回=红叉 */}
      <span style={{ fontSize: 16, lineHeight: '22px' }}>{meta.icon}</span>

      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <Typography.Text
            strong={unread}
            style={{ fontSize: 13, color: unread ? '#1f1f1f' : '#595959' }}
            ellipsis
          >
            {item.title}
          </Typography.Text>
          {/* 未读圆点：与角标呼应，便于在列表中定位新内容 */}
          {unread && (
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                background: meta.color,
                flexShrink: 0,
              }}
            />
          )}
        </div>

        <Typography.Paragraph
          type="secondary"
          style={{ fontSize: 12, margin: '2px 0 0', whiteSpace: 'pre-wrap' }}
          // 正文可能含驳回理由（多行），限制为 3 行，完整内容见「我的文档」
          ellipsis={{ rows: 3 }}
        >
          {item.content}
        </Typography.Paragraph>

        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          {formatRelative(item.created_at)}
        </Typography.Text>
      </div>
    </div>
  );
}
