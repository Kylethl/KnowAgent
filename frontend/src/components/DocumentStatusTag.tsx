import { Progress, Tag, Typography } from 'antd';
import type { DocStatus, UploadItem } from '../types';

// P1-F3：上传项状态标签
// 把「本地上传状态」映射为用户能看懂的展示：
//   uploading → 实时进度条（含服务端处理中的等待态）
//   success   → 按后端返回的文档状态展示标签（P1 只会出现「待审核」）
//   error     → 「上传失败」标签

// 文档状态 → 标签文案与颜色
// 取值覆盖后端 documents.status 的全部 5 种（见 sql/002_p1_db2_documents.sql）；
// P1 阶段只会出现 pending，其余为 P3 审核/入库流程预留，避免届时再改组件。
const STATUS_META: Record<DocStatus, { label: string; color: string }> = {
  pending: { label: '待审核', color: 'gold' },
  approved: { label: '审核通过', color: 'cyan' },
  indexing: { label: '入库中', color: 'blue' },
  indexed: { label: '已入库', color: 'green' },
  rejected: { label: '已驳回', color: 'red' },
};

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
    return <Tag color={meta.color}>{meta.label}</Tag>;
  }

  // 上传失败
  return <Tag color="error">上传失败</Tag>;
}
