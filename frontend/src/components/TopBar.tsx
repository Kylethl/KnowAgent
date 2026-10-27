import { useState } from 'react';
import { Button, Dropdown, Space, Typography } from 'antd';
import {
  CloudUploadOutlined,
  LogoutOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@ant-design/icons';
import type { AuthUser } from '../types';

// 顶部标题栏组件（豆包风格：左侧品牌图标 + 产品名 + 副标题，右侧操作区）
// 高度固定，作为对话页骨架的顶部区域。
//
// P1-F3：右侧新增「上传文档」入口，点击后由父组件打开上传抽屉。
// P2-F2：右侧新增「当前用户 + 登出」入口，点击用户名可展开菜单执行登出。
// P3-F1：管理员账号额外显示「管理后台」入口（普通用户不渲染，避免误导点击）。
// 小功能：左侧品牌区（图标 + 「知识库问答」）在传入 onHomeClick 时可点击返回对话页，
//       例如从管理后台点它即可回到对话界面；对话页自身不传该回调，故品牌区不可点击。
interface Props {
  onUploadClick?: () => void; // 点击「上传文档」按钮的回调
  user?: AuthUser | null; // 当前登录用户（P2-F2；未登录时为 null）
  onLogout?: () => void; // 点击「退出登录」的回调
  onAdminClick?: () => void; // 点击「管理后台」的回调（P3-F1；仅管理员且传入时显示入口）
  onHomeClick?: () => void; // 点击左侧品牌区的回调（传入后品牌区才可点击，用于返回对话页）
}

export default function TopBar({
  onUploadClick,
  user,
  onLogout,
  onAdminClick,
  onHomeClick,
}: Props) {
  // 品牌区悬停态：仅在可点击时用于高亮背景（style 对象无法写 :hover，故用状态驱动）
  const [brandHover, setBrandHover] = useState(false);
  // 是否可点击回对话页：由父组件是否传入回调决定
  const homeClickable = Boolean(onHomeClick);

  return (
    <header
      style={{
        height: 56, // 固定高度
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '0 20px',
        borderBottom: '1px solid #ececec', // 与下方内容区分隔
        background: '#fff',
      }}
    >
      {/* 品牌区：机器人图标 + 产品名 + 副标题。
          传入 onHomeClick 时整块可点击（返回对话页），并带悬停高亮与提示文案；
          未传入（如对话页自身）则保持纯展示，不可点击。 */}
      <div
        role={homeClickable ? 'button' : undefined}
        tabIndex={homeClickable ? 0 : undefined}
        title={homeClickable ? '返回对话界面' : undefined}
        onClick={onHomeClick}
        // 键盘可达：回车/空格等同于点击
        onKeyDown={(e) => {
          if (!homeClickable) return;
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onHomeClick?.();
          }
        }}
        onMouseEnter={() => setBrandHover(true)}
        onMouseLeave={() => setBrandHover(false)}
        onFocus={() => setBrandHover(true)}
        onBlur={() => setBrandHover(false)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          // 负 margin 抵消内边距，保证视觉上仍贴左边缘
          marginLeft: -8,
          padding: '4px 8px',
          borderRadius: 8,
          cursor: homeClickable ? 'pointer' : 'default',
          background: homeClickable && brandHover ? '#f0f4ff' : 'transparent',
          transition: 'background 0.15s ease',
          // 去掉聚焦时的浏览器默认描边，改用背景高亮表达
          outline: 'none',
        }}
      >
        {/* 品牌图标：机器人，呼应「AI 助手」定位 */}
        <RobotOutlined style={{ fontSize: 22, color: '#386BF3' }} />
        {/* 产品主标题 */}
        <Typography.Title level={5} style={{ margin: 0 }}>
          知识库问答
        </Typography.Title>
        {/* 副标题：说明产品性质 */}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          个人知识库 · AI 助手
        </Typography.Text>
      </div>

      {/* 右侧操作区：marginLeft:auto 把它推到栏尾 */}
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 12 }}>
        {/* P1-F3：上传入口（管理后台页不传该回调，故不渲染） */}
        {onUploadClick && (
          <Button icon={<CloudUploadOutlined />} onClick={onUploadClick}>
            上传文档
          </Button>
        )}

        {/* P3-F1：管理后台入口——仅管理员账号且父组件传入回调时显示 */}
        {user?.role === 'admin' && onAdminClick && (
          <Button icon={<SafetyCertificateOutlined />} onClick={onAdminClick}>
            管理后台
          </Button>
        )}

        {/* P2-F2：当前用户 + 登出；仅在已登录（拿到 user）时渲染 */}
        {user && (
          <Dropdown
            // 菜单项：退出登录。key 与 onClick 的 key 对应
            menu={{
              items: [
                {
                  key: 'logout',
                  icon: <LogoutOutlined />,
                  label: '退出登录',
                },
              ],
              onClick: ({ key }) => {
                if (key === 'logout') onLogout?.();
              },
            }}
            placement="bottomRight"
          >
            {/* 触发区：用户名（附带角色标签便于后续 admin 识别） */}
            <Space size={6} style={{ cursor: 'pointer' }}>
              <UserOutlined style={{ color: '#386BF3' }} />
              <Typography.Text style={{ fontSize: 13 }}>{user.username}</Typography.Text>
            </Space>
          </Dropdown>
        )}
      </div>
    </header>
  );
}
