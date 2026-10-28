import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Drawer, Empty, Spin, Tag, Typography } from 'antd';
import {
  AudioOutlined,
  DeleteOutlined,
  FileMarkdownOutlined,
  FileWordOutlined,
  PictureOutlined,
  RedoOutlined,
  ReloadOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons';
import { useDocumentUpload } from '../hooks/useDocumentUpload';
import { formatFileSize } from '../services/documentUpload';
import { getMyDocuments } from '../services/documentApi';
import type { DocType, MyDocument, UploadItem } from '../types';
import DocumentStatusTag, { DocumentStatusBadge } from './DocumentStatusTag';
import UploadDropzone from './UploadDropzone';

// P1-F3 / P5-F1：文档上传面板（右侧抽屉）
// 组成：业务说明 → 拖拽上传区 → 上传列表（进度 / 待审核标签 / 重试 / 移除）。
// 对应《需求清单》P5 功能1 的前端部分：
//   「上传入口扩展，接受图片/音频/视频，显示类型标签与状态」

interface Props {
  open: boolean; // 抽屉是否展开
  onClose: () => void; // 关闭回调
}

// 文档类型 → 展示文案、颜色与图标（P1 两种 + P5 三种）
// 图标与配色参考 antd 的语义色：Word 蓝、Markdown 浅蓝、图片绿、音频橙、视频紫
const TYPE_META: Record<DocType, { label: string; color: string; icon: JSX.Element }> = {
  word: { label: 'Word', color: '#2b579a', icon: <FileWordOutlined /> },
  markdown: { label: 'Markdown', color: '#5191d1', icon: <FileMarkdownOutlined /> },
  image: { label: '图片', color: '#389e0d', icon: <PictureOutlined /> },
  audio: { label: '音频', color: '#d46b08', icon: <AudioOutlined /> },
  video: { label: '视频', color: '#722ed1', icon: <VideoCameraOutlined /> },
};

/** 把秒数格式化为「m:ss」或「h:mm:ss」，未知返回 null。 */
function formatDuration(seconds?: number | null): string | null {
  if (!seconds || seconds <= 0) return null;
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export default function UploadPanel({ open, onClose }: Props) {
  // 上传队列的全部状态与操作均来自钩子，本组件只负责渲染
  const { items, error, addFiles, removeItem, retry, clearAll, clearError } =
    useDocumentUpload();

  // 已提交成功的文档数：即处于「待审核」状态的文档数
  const pendingCount = useMemo(
    () => items.filter((it) => it.status === 'success').length,
    [items]
  );

  // 「我的文档」：本人提交的文档列表（含入库状态）。
  // 抽屉打开时拉取一次；提供刷新按钮手动刷新（如刚上传/管理员刚入库）。
  const [myDocs, setMyDocs] = useState<MyDocument[]>([]);
  const [myLoading, setMyLoading] = useState(false);
  const [myError, setMyError] = useState<string | null>(null);

  /** 拉取「我的文档」列表（出错只提示，不影响上传区使用）。 */
  const fetchMyDocs = useCallback(async () => {
    setMyLoading(true);
    setMyError(null);
    try {
      const res = await getMyDocuments();
      setMyDocs(res.items);
    } catch (err) {
      setMyError(err instanceof Error ? err.message : '加载我的文档失败');
    } finally {
      setMyLoading(false);
    }
  }, []);

  // 抽屉每次打开都重新拉取，保证看到最新状态
  useEffect(() => {
    if (open) void fetchMyDocs();
  }, [open, fetchMyDocs]);

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
      {/* 业务说明：让用户理解提交后的流程（P1 提交 → P3 审核 → P3 入库） */}
      <Alert
        type="info"
        showIcon
        message="文档提交后进入「待审核」状态"
        description="管理员审核通过并完成解析入库后，即可参与知识库问答。下方「我的文档」可查看每篇文档的审核/入库进度。"
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

      {/* 「我的文档」：本人提交的文档及其审核/入库状态（P3-B2 状态展示） */}
      <div style={{ marginTop: 24 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 12,
          }}
        >
          <Typography.Title level={5} style={{ margin: 0 }}>
            我的文档
          </Typography.Title>
          <Button
            size="small"
            type="text"
            icon={<ReloadOutlined />}
            onClick={() => void fetchMyDocs()}
            loading={myLoading}
          >
            刷新
          </Button>
        </div>

        {myLoading ? (
          // 首屏加载：骨架占位，避免抽屉空白
          <div style={{ textAlign: 'center', padding: '16px 0' }}>
            <Spin />
          </div>
        ) : myError ? (
          // 加载失败：给出原因，不阻断上传区
          <Alert type="warning" showIcon message={myError} />
        ) : myDocs.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有提交过文档"
            style={{ padding: '12px 0' }}
          />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {myDocs.map((doc) => (
              <MyDocRow key={doc.id} doc={doc} />
            ))}
          </div>
        )}
      </div>
    </Drawer>
  );
}

/**
 * 「我的文档」中的一行：文件名 / 类型 / 大小 + 状态徽标 + 切片数 / 入库时间 / 失败原因。
 */
function MyDocRow({ doc }: { doc: MyDocument }) {
  const meta = TYPE_META[doc.file_type];

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

      {/* 中间信息区：文件名 + 类型标签 + 大小 + 备注（驳回理由 / 入库失败原因） */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span
            title={doc.filename}
            style={{
              maxWidth: 200,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              fontSize: 14,
            }}
          >
            {doc.filename}
          </span>
          <Tag color="blue" style={{ marginInlineEnd: 0 }}>
            {meta.label}
          </Tag>
        </div>
        <div style={{ marginTop: 2 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {formatFileSize(doc.size_bytes)}
            {doc.chunk_count > 0 && ` · 切片 ${doc.chunk_count} 段`}
            {/* P5：音视频显示时长，图片/音视频显示 OCR/ASR 识别出的字符数 */}
            {formatDuration(doc.extract_meta?.duration_seconds) &&
              ` · 时长 ${formatDuration(doc.extract_meta?.duration_seconds)}`}
            {typeof doc.extract_meta?.chars === 'number' && ` · 识别 ${doc.extract_meta.chars} 字`}
            {doc.indexed_at && ` · 已入库 ${formatMyTime(doc.indexed_at)}`}
          </Typography.Text>
        </div>
        {/* 驳回理由：以红字提示，便于用户修改后重新提交 */}
        {doc.review_status === 'rejected' && doc.review_note && (
          <div style={{ marginTop: 2, color: '#ff4d4f', fontSize: 12 }}>驳回理由：{doc.review_note}</div>
        )}
        {/* P4：向量过期提示 —— 文档虽显示「已入库」，但向量与当前模型不可比，
            检索会失效或召回错误内容。用户无法自行重新入库，故引导联系管理员。 */}
        {doc.embedding_stale && (
          <div style={{ marginTop: 2, color: '#d46b08', fontSize: 12 }}>
            向量已过期（入库模型：{doc.embedding_model ?? '未知'}），暂不参与知识库检索，
            请联系管理员「重新入库」。
          </div>
        )}
      </div>

      {/* 右侧状态徽标：直接按文档生命周期状态渲染（含入库失败）；
          媒体类在入库阶段显示「解析中」（OCR/ASR 比文本解析慢，文案要贴合实际） */}
      <DocumentStatusBadge
        status={doc.status}
        lastIndexError={doc.last_index_error}
        fileType={doc.file_type}
      />
    </div>
  );
}

/** 把 ISO 时间字符串格式化为「年-月-日 时:分」；空/非法值返回占位符。 */
function formatMyTime(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
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
