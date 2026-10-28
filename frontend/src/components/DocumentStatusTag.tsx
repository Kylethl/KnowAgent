import { Progress, Tag, Tooltip, Typography } from 'antd';
import type { DocStatus, DocType, UploadItem } from '../types';

// P1-F3：上传项状态标签
// 把「本地上传状态」映射为用户能看懂的展示：
//   uploading → 实时进度条（含服务端处理中的等待态）
//   success   → 按后端返回的文档状态展示标签（P1 只会出现「待审核」）
//   error     → 「上传失败」标签
//
// P5：图片/音频/视频在入库阶段要做 OCR/ASR，这一步耗时明显长于文本解析，
//     因此入库中的文案按类型区分——媒体显示「解析中」，让用户知道在等模型而不是卡住。

// 文档状态 → 标签文案与颜色
// 取值覆盖后端 documents.status 的全部 5 种（见 sql/002_p1_db2_documents.sql）；
// P1 阶段只会出现 pending，其余为 P3 审核/入库流程预留，避免届时再改组件。
// 导出供「我的文档」等纯状态展示场景复用（见 DocumentStatusBadge）。
export const STATUS_META: Record<DocStatus, { label: string; color: string }> = {
  pending: { label: '待审核', color: 'gold' },
  approved: { label: '审核通过', color: 'cyan' },
  indexing: { label: '入库中', color: 'blue' },
  indexed: { label: '已入库', color: 'green' },
  rejected: { label: '已驳回', color: 'red' },
};

// 媒体类型集合：用于判断是否需要展示「解析中」
const MEDIA_TYPES: readonly DocType[] = ['image', 'audio', 'video'];

/**
 * 按「状态 + 文件类型」给出展示文案。
 * 媒体类在 indexing 阶段实际是 OCR/ASR 解析，文案用「解析中」更准确。
 */
export function statusLabel(status: DocStatus, fileType?: DocType | null): string {
  if (status === 'indexing' && fileType && MEDIA_TYPES.includes(fileType)) {
    return '解析中';
  }
  return STATUS_META[status].label;
}

/**
 * 直接展示文档生命周期状态的徽标（不依赖上传队列项）。
 * 供「我的文档」列表、管理员后台状态列等「纯状态」场景复用。
 *
 * 说明：后端入库失败并不会产生独立的 status 值，而是回退为 'approved'
 *       并写入 last_index_error，因此需结合该字段区分「已通过待入库」与「入库失败」。
 *
 * @param status 文档生命周期状态
 * @param lastIndexError 最近一次入库失败原因（成功为空）；非空即视为「入库失败」
 * @param fileType 文档类型（媒体类会把「入库中」显示为「解析中」）
 */
export function DocumentStatusBadge({
  status,
  lastIndexError,
  fileType,
}: {
  status: DocStatus;
  lastIndexError?: string | null;
  fileType?: DocType | null;
}) {
  // 入库失败：status 已回退为 approved，用 lastIndexError 非空来判定
  if ((status === 'approved' || status === 'indexing') && lastIndexError) {
    return (
      <Tooltip title={lastIndexError}>
        <Tag color="red">入库失败</Tag>
      </Tooltip>
    );
  }
  const meta = STATUS_META[status];
  return <Tag color={meta.color}>{statusLabel(status, fileType)}</Tag>;
}


interface Props {
  item: UploadItem; // 上传队列中的一项
}

export default function DocumentStatusTag({ item }: Props) {
  // 上传中：展示进度条 + 百分比；到达传输上限后提示「处理中」，
  // 因为此时字节已发完，正在等后端写对象存储与落库。
  if (item.status === 'uploading') {
    const waitingServer = item.percent >= 95;
    return (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        <Progress
          percent={item.percent}
          size="small"
          style={{ width: 110, marginBottom: 0 }}
          // 等待服务端响应时不显示百分比数字，避免卡在 95% 造成误解
          format={waitingServer ? () => '' : undefined}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
          {waitingServer ? '处理中' : `上传中 ${item.percent}%`}
        </Typography.Text>
      </span>
    );
  }

  // 上传成功：以服务端返回的文档状态为准；缺失时回退到 pending（待审核）
  if (item.status === 'success') {
    const docStatus: DocStatus = item.record?.status ?? 'pending';
    const meta = STATUS_META[docStatus];
    // 媒体类在入库阶段显示「解析中」，与「我的文档」列表保持一致
    return <Tag color={meta.color}>{statusLabel(docStatus, item.docType)}</Tag>;
  }

  // 上传失败
  return <Tag color="error">上传失败</Tag>;
}
