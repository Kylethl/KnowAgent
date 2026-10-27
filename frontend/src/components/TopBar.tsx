import { Button, Typography } from 'antd';
import { CloudUploadOutlined, RobotOutlined } from '@ant-design/icons';

// 顶部标题栏组件（豆包风格：左侧品牌图标 + 产品名 + 副标题，右侧操作区）
// 高度固定，作为对话页骨架的顶部区域。
//
// P1-F3：右侧新增「上传文档」入口，点击后由父组件打开上传抽屉。
interface Props {
  onUploadClick?: () => void; // 点击「上传文档」按钮的回调
}

export default function TopBar({ onUploadClick }: Props) {
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

      {/* P1-F3：右侧上传入口；marginLeft:auto 把它推到栏尾 */}
      <div style={{ marginLeft: 'auto' }}>
        <Button icon={<CloudUploadOutlined />} onClick={onUploadClick}>
          上传文档
        </Button>
      </div>
    </header>
  );
}
