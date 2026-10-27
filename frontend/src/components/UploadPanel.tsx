import { useMemo } from 'react';
import { Alert, Button, Drawer, Empty, Tag, Typography } from 'antd';
import {
  DeleteOutlined,
  FileMarkdownOutlined,
  FileWordOutlined,
  RedoOutlined,
} from '@ant-design/icons';
import { useDocumentUpload } from '../hooks/useDocumentUpload';
import { formatFileSize } from '../services/documentUpload';
import type { DocType, UploadItem } from '../types';
import DocumentStatusTag from './DocumentStatusTag';
import UploadDropzone from './UploadDropzone';

// P1-F3：文档上传面板（右侧抽屉）
// 组成：业务说明 → 拖拽上传区 → 上传列表（进度 / 待审核标签 / 重试 / 移除）。
// 对应《需求清单》P1 功能2 的前端部分：
//   「上传入口（拖拽/选择），仅接受 .docx/.doc/.md，显示上传中状态与『待审核』标签」

interface Props {
  open: boolean; // 抽屉是否展开
  onClose: () => void; // 关闭回调
}

// 文档类型 → 展示文案与图标（P1 仅两种类型）
const TYPE_META: Record<DocType, { label: string; color: string; icon: JSX.Element }> = {
  word: { label: 'Word', color: '#2b579a', icon: <FileWordOutlined /> },
  markdown: { label: 'Markdown', color: '#5191d1', icon: <FileMarkdownOutlined /> },
};

export default function UploadPanel({ open, onClose }: Props) {
  // 上传队列的全部状态与操作均来自钩子，本组件只负责渲染
  const { items, error, addFiles, removeItem, retry, clearAll, clearError } =
    useDocumentUpload();

  // 已提交成功的文档数：即处于「待审核」状态的文档数
  const pendingCount = useMemo(
    () => items.filter((it) => it.status === 'success').length,
    [items]
  );

  return (
    <Drawer
      title="上传文档"
      placement="right"
      width={520}
      open={open}
      onClose={onClose}
      // 底部操作区：仅在有内容时出现「清空列表」
      footer={
        items.length > 0 ? (
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              共 {items.length} 个文件，已提交 {pendingCount} 个
            </Typography.Text>
            <Button onClick={clearAll}>清空列表</Button>
          </div>
        ) : null
      }
    >
      {/* 业务说明：让用户理解提交后的流程（P1 仅落库待审核，P3 才审核入库） */}
      <Alert
        type="info"
        showIcon
        message="文档提交后进入「待审核」状态"
        description="管理员审核通过并完成解析入库后，才会参与知识库问答（审核与入库在后续阶段上线）。"
        style={{ marginBottom: 16 }}
      />

      <UploadDropzone onFiles={addFiles} />

      {/* 批次级校验错误：如格式不支持、超过数量上限 */}
      {error && (
        <Alert
          type="warning"
          showIcon
          closable
          message={error}
          style={{ marginTop: 12 }}
          onClose={clearError} // 用户手动关闭后清空错误，避免下次上传仍显示旧提示
        />
      )}

      {/* 上传列表 */}
      <div style={{ marginTop: 16 }}>
        {items.length === 0 ? (
          // 空态：引导用户操作
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有上传任何文档"
            style={{ padding: '24px 0' }}
          />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {items.map((item) => (
              <UploadRow
                key={item.id}
                item={item}
                onRemove={() => removeItem(item.id)}
                onRetry={() => retry(item.id)}
              />
            ))}
          </div>
        )}
      </div>
    </Drawer>
  );
}

/**
 * 上传列表中的一行：文件名 / 类型 / 大小 + 状态标签 + 操作按钮
 */
function UploadRow({
  item,
  onRemove,
  onRetry,
}: {
  item: UploadItem;
  onRemove: () => void; // 从列表移除（传输中会一并中断）
  onRetry: () => void; // 重新上传失败的项
}) {
  const meta = TYPE_META[item.docType];

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '10px 12px',
        border: '1px solid #f0f0f0',
        borderRadius: 10,
        background: '#fff',
      }}
    >
      {/* 类型图标：按文档类型着色，便于快速区分 Word 与 Markdown */}
      <span style={{ fontSize: 22, color: meta.color }}>{meta.icon}</span>

      {/* 中间信息区：文件名 + 类型标签 + 大小 + 失败原因 */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {/* 文件名过长时截断，避免撑破抽屉布局 */}
          <span
            title={item.file.name}
            style={{
              maxWidth: 200,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              fontSize: 14,
            }}
          >
            {item.file.name}
          </span>
          <Tag color="blue" style={{ marginInlineEnd: 0 }}>
            {meta.label}
          </Tag>
        </div>
        <div style={{ marginTop: 2 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {formatFileSize(item.file.size)}
          </Typography.Text>
        </div>
        {/* 失败原因：仅失败态展示，红字提示具体原因 */}
        {item.status === 'error' && item.error && (
          <div style={{ marginTop: 2, color: '#ff4d4f', fontSize: 12 }}>{item.error}</div>
        )}
      </div>

      {/* 右侧状态区：进度条 / 待审核 / 失败 */}
      <DocumentStatusTag item={item} />

      {/* 操作区：失败可重试，任何状态都可移除 */}
      <div style={{ display: 'flex', gap: 2 }}>
        {item.status === 'error' && (
          <Button type="text" size="small" icon={<RedoOutlined />} onClick={onRetry} title="重试" />
        )}
        <Button
          type="text"
          size="small"
          icon={<DeleteOutlined />}
          onClick={onRemove}
          title="移除"
        />
      </div>
    </div>
  );
}
