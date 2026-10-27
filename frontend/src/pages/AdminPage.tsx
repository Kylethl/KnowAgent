import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Button,
  Drawer,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  ArrowLeftOutlined,
  CheckOutlined,
  CloseOutlined,
  EyeOutlined,
  ReloadOutlined,
  StopOutlined,
  FileTextOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import TopBar from '../components/TopBar';
import { useAuth } from '../hooks/useAuth';
import { listAdminDocuments, previewDocument, reviewDocument } from '../services/adminApi';
import { adminReindexDocument, adminUnpublishDocument } from '../services/documentApi';
import type { AdminDocument, DocStatus, DocumentPreview, ReviewStatus } from '../types';

// P3-F1：管理员审核后台页
// ---------------------------------------------------------------------
// 契约来源：《需求清单》P3 功能1「管理员审核」
//   - 前端：管理员后台页、待审核列表、文档预览、通过/驳回（驳回必填理由）
//   - 后端：GET /admin/documents、POST /admin/documents/{id}/review、
//          GET /admin/documents/{id}/preview
//
// 页面结构：
//   顶部 TopBar（复用对话页顶栏，提供返回入口）
//   筛选区：审核结论 Segmented + 文件名搜索
//   列表区：Table 展示文档，含「预览 / 通过 / 驳回」操作
//   预览：Drawer 展示文档文本（超长截断提示）
//   驳回：Modal 填写理由（前端必填校验 + 后端二次校验）
//
// 权限说明：进入本页前已由 RequireAdmin 保证 role='admin'；
//           接口层仍由后端 require_admin 兜底，前端不做最终裁决。

// 审核结论筛选选项：'all' 为前端约定的「不过滤」值，不传给后端
type FilterValue = ReviewStatus | 'all';

/** 生命周期状态 → 展示标签的颜色与文案（对齐后端 status 取值集合）。 */
const STATUS_META: Record<DocStatus, { color: string; text: string }> = {
  pending: { color: 'orange', text: '待审核' },
  approved: { color: 'green', text: '已通过' },
  rejected: { color: 'red', text: '已驳回' },
  indexing: { color: 'blue', text: '入库中' },
  indexed: { color: 'cyan', text: '已入库' },
};

/** 把字节数格式化为可读文本（B / KB / MB）。 */
function formatSize(bytes: number): string {
  if (!bytes || bytes < 1024) return `${bytes || 0} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 把 ISO 时间字符串格式化为「年-月-日 时:分」；空值返回占位符。 */
function formatTime(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  // 非法时间（脏数据）时原样返回，避免显示 Invalid Date
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function AdminPage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate(); // 用于「返回对话」：从后台跳回对话页（/）

  /** 返回对话界面：顶栏品牌区与「返回对话」按钮共用该跳转。 */
  const handleBackToChat = useCallback(() => {
    navigate('/');
  }, [navigate]);

  // ---------- 列表与筛选状态 ----------
  const [filter, setFilter] = useState<FilterValue>('pending'); // 默认看待审核，贴合管理员日常动线
  const [keywordInput, setKeywordInput] = useState(''); // 搜索框输入（受控，回车/点击才提交）
  const [keyword, setKeyword] = useState(''); // 实际生效的搜索关键字
  const [items, setItems] = useState<AdminDocument[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);

  // ---------- 预览抽屉状态 ----------
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [preview, setPreview] = useState<DocumentPreview | null>(null);

  // ---------- 驳回弹窗状态 ----------
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectTarget, setRejectTarget] = useState<AdminDocument | null>(null);
  const [rejectNote, setRejectNote] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // ---------- 入库操作状态（重新入库 / 下线） ----------
  const [indexingId, setIndexingId] = useState<string | null>(null); // 正在执行入库操作的文档 ID

  /**
   * 拉取审核队列。
   * 用 useCallback 包裹，便于作为 useEffect 依赖且避免重复创建。
   */
  const fetchList = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listAdminDocuments({
        // 'all' 是前端占位值，后端只需在 None 时返回全部，故此处不传
        reviewStatus: filter === 'all' ? undefined : filter,
        keyword: keyword || undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      // 后端 detail 已在 ApiError.message 中，直接展示即可
      message.error(err instanceof Error ? err.message : '加载审核队列失败');
    } finally {
      setLoading(false);
    }
  }, [filter, keyword, page, pageSize]);

  // 筛选条件或分页变化时重新拉取（keyword 由搜索动作触发变更，不随输入抖动）
  useEffect(() => {
    void fetchList();
  }, [fetchList]);

  /** 切换筛选时重置到第一页，避免停留在越界页码上看到空列表。 */
  const handleFilterChange = (value: FilterValue) => {
    setFilter(value);
    setPage(1);
  };

  /** 搜索：把输入框内容提交为生效关键字，并回到第一页。 */
  const handleSearch = () => {
    setKeyword(keywordInput.trim());
    setPage(1);
  };

  /**
   * 打开预览抽屉并加载文档文本。
   * @param doc 目标文档
   */
  const handlePreview = async (doc: AdminDocument) => {
    setPreviewOpen(true);
    setPreviewLoading(true);
    setPreview(null);
    try {
      setPreview(await previewDocument(doc.id));
    } catch (err) {
      message.error(err instanceof Error ? err.message : '预览失败');
    } finally {
      setPreviewLoading(false);
    }
  };

  /**
   * 审核通过（直接提交，无需额外输入）。
   * @param doc 目标文档
   */
  const handleApprove = async (doc: AdminDocument) => {
    setSubmitting(true);
    try {
      await reviewDocument(doc.id, { action: 'approved' });
      message.success(`已通过《${doc.filename}》`);
      // 重新拉取，保证列表与各筛选项计数同步
      await fetchList();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '审核失败');
    } finally {
      setSubmitting(false);
    }
  };

  /**
   * 打开驳回弹窗（预置目标文档与空理由）。
   * @param doc 目标文档
   */
  const openReject = (doc: AdminDocument) => {
    setRejectTarget(doc);
    setRejectNote('');
    setRejectOpen(true);
  };

  /** 提交驳回：前端先校验理由非空（后端也会二次校验，双保险）。 */
  const submitReject = async () => {
    const note = rejectNote.trim();
    if (!note) {
      message.warning('驳回时必须填写理由，便于提交人修改');
      return;
    }
    if (!rejectTarget) return;

    setSubmitting(true);
    try {
      await reviewDocument(rejectTarget.id, { action: 'rejected', note });
      message.success(`已驳回《${rejectTarget.filename}》`);
      setRejectOpen(false);
      await fetchList();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '驳回失败');
    } finally {
      setSubmitting(false);
    }
  };

  /**
   * 重新入库：对已审核通过的文档重跑解析→切片→向量化（P3-B2）。
   * 适用场景：入库失败后重试、或文档内容更新后重建索引。
   * 接口为「提交即返回」，真正的入库在后台异步进行，结果体现在文档状态上。
   * @param doc 目标文档
   */
  const handleReindex = async (doc: AdminDocument) => {
    setIndexingId(doc.id);
    try {
      const res = await adminReindexDocument(doc.id);
      message.success(
        res.queued
          ? `已提交《${doc.filename}》入库任务（入库中）`
          : `《${doc.filename}》已开始入库`,
      );
      // 重新拉取，使状态列/切片数同步刷新
      await fetchList();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '重新入库失败');
    } finally {
      setIndexingId(null);
    }
  };

  /**
   * 下线：删除该文档的全部向量切片，使其不再参与检索（P3-B2）。
   * 审核结论保持不变，之后可再次入库。
   * @param doc 目标文档
   */
  const handleUnpublish = async (doc: AdminDocument) => {
    setIndexingId(doc.id);
    try {
      await adminUnpublishDocument(doc.id);
      message.success(`已下线《${doc.filename}》（向量切片已清理，可再次入库）`);
      await fetchList();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '下线失败');
    } finally {
      setIndexingId(null);
    }
  };

  /** 表格列定义：用 useMemo 避免每次渲染重建数组。 */
  const columns = useMemo<ColumnsType<AdminDocument>>(
    () => [
      {
        title: '文件名',
        dataIndex: 'filename',
        ellipsis: true,
        render: (text: string) => (
          <Space size={6}>
            <FileTextOutlined style={{ color: '#386BF3' }} />
            <span title={text}>{text}</span>
          </Space>
        ),
      },
      {
        title: '类型',
        dataIndex: 'file_type',
        width: 90,
        render: (t: string) => (t === 'word' ? 'Word' : 'Markdown'),
      },
      {
        title: '大小',
        dataIndex: 'size_bytes',
        width: 90,
        render: (v: number) => formatSize(v),
      },
      {
        title: '提交人',
        dataIndex: 'uploader',
        width: 120,
        // 提交人已注销时后端返回 null，展示占位符
        render: (v: string | null) => v ?? '（已注销）',
      },
      {
        title: '提交时间',
        dataIndex: 'created_at',
        width: 150,
        render: (v: string) => formatTime(v),
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 100,
        render: (s: DocStatus, row) => {
          // 入库失败并不会产生独立的 status 值（会回退为 approved），
          // 需结合 last_index_error 判定，并 hover 展示失败原因。
          if ((s === 'approved' || s === 'indexing') && row.last_index_error) {
            return (
              <Tooltip title={row.last_index_error}>
                <Tag color="red">入库失败</Tag>
              </Tooltip>
            );
          }
          const meta = STATUS_META[s] ?? { color: 'default', text: s };
          return <Tag color={meta.color}>{meta.text}</Tag>;
        },
      },
      {
        title: '切片数',
        dataIndex: 'chunk_count',
        width: 100,
        render: (v: number, row) =>
          // >0 表示已成功写入向量切片；hover 展示入库时间
          v > 0 ? (
            <Tooltip title={row.indexed_at ? `入库时间：${formatTime(row.indexed_at)}` : ''}>
              <Tag color="green">{v} 段</Tag>
            </Tooltip>
          ) : (
            '—'
          ),
      },
      {
        title: '审核信息',
        dataIndex: 'review_note',
        ellipsis: true,
        // 已审核时展示「审核人 + 理由」；未审核展示占位
        render: (note: string | null, row) =>
          row.review_status === 'pending' ? (
            <Typography.Text type="secondary">未审核</Typography.Text>
          ) : (
            <span title={note ?? ''}>
              {row.reviewer ?? '—'}
              {note ? `：${note}` : ''}
            </span>
          ),
      },
      {
        title: '操作',
        key: 'action',
        width: 340,
        fixed: 'right',
        render: (_, row) => (
          <Space size={4} wrap>
            {/* 预览：任何状态都可查看内容 */}
            <Button size="small" icon={<EyeOutlined />} onClick={() => void handlePreview(row)}>
              预览
            </Button>
            {/* 通过：已通过的文档后端会拒绝（409），这里同步置灰避免无效操作 */}
            <Button
              size="small"
              type="primary"
              icon={<CheckOutlined />}
              disabled={row.review_status === 'approved'}
              loading={submitting}
              onClick={() => void handleApprove(row)}
            >
              通过
            </Button>
            {/* 驳回：同上，已通过时置灰 */}
            <Button
              size="small"
              danger
              icon={<CloseOutlined />}
              disabled={row.review_status === 'approved'}
              onClick={() => openReject(row)}
            >
              驳回
            </Button>
            {/* 重新入库：仅审核通过且未在入库中的文档可触发；入库中禁用避免并发 */}
            <Button
              size="small"
              icon={<ReloadOutlined />}
              disabled={row.status === 'indexing' || row.review_status !== 'approved'}
              loading={indexingId === row.id}
              onClick={() => void handleReindex(row)}
            >
              重新入库
            </Button>
            {/* 下线：删除向量切片；仅在非入库中时允许（入库中会与后台任务冲突） */}
            <Popconfirm
              title="确认下线该文档？"
              description="将删除其全部向量切片，不再参与检索；可再次入库。"
              okText="确认下线"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => void handleUnpublish(row)}
            >
              <Button
                size="small"
                icon={<StopOutlined />}
                disabled={row.status === 'indexing'}
              >
                下线
              </Button>
            </Popconfirm>
          </Space>
        ),
      },
    ],
    // fetchList / submitting / indexingId 参与依赖，保证回调拿到最新状态
    [fetchList, submitting, indexingId],
  );

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column', background: '#f7f8fa' }}>
      {/* 顶栏：复用对话页顶栏；管理后台不需要上传入口，故不传 onUploadClick。
          传入 onHomeClick 后，点击左上角图标或「知识库问答」即可返回对话页。 */}
      <TopBar user={user} onLogout={logout} onHomeClick={handleBackToChat} />

      <main style={{ flex: 1, padding: 24, maxWidth: 1200, width: '100%', margin: '0 auto' }}>
        {/* 标题区：左侧标题，右侧提供显式的「返回对话」入口（与顶栏品牌区等效） */}
        <div
          style={{
            marginBottom: 16,
            display: 'flex',
            alignItems: 'flex-end',
            justifyContent: 'space-between',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <div>
            <Typography.Title level={4} style={{ margin: 0 }}>
              管理员审核后台
            </Typography.Title>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              审核用户提交的 Word / Markdown 文档；通过后进入入库流程（P3 功能2）
            </Typography.Text>
          </div>
          <Button icon={<ArrowLeftOutlined />} onClick={handleBackToChat}>
            返回对话
          </Button>
        </div>

        {/* 筛选区：审核结论切换 + 文件名搜索 + 手动刷新 */}
        <Space wrap style={{ marginBottom: 16 }}>
          <Segmented<FilterValue>
            value={filter}
            onChange={handleFilterChange}
            options={[
              { label: '待审核', value: 'pending' },
              { label: '已通过', value: 'approved' },
              { label: '已驳回', value: 'rejected' },
              { label: '全部', value: 'all' },
            ]}
          />
          <Input.Search
            placeholder="按文件名搜索"
            allowClear
            style={{ width: 240 }}
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            onSearch={handleSearch}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void fetchList()}>
            刷新
          </Button>
        </Space>

        {/* 列表区：分页交给后端，total 由接口返回 */}
        <Table<AdminDocument>
          rowKey="id"
          columns={columns}
          dataSource={items}
          loading={loading}
          scroll={{ x: 1000 }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
        />
      </main>

      {/* 预览抽屉：展示文档提取出的纯文本 */}
      <Drawer
        title={preview ? `预览：${preview.filename}` : '预览'}
        width={640}
        open={previewOpen}
        onClose={() => setPreviewOpen(false)}
      >
        {previewLoading ? (
          <Typography.Text type="secondary">正在加载预览…</Typography.Text>
        ) : preview ? (
          <>
            {/* 不可预览（旧版 .doc / 文件损坏）时给出明确提示 */}
            {!preview.previewable && (
              <Typography.Text type="warning">{preview.text}</Typography.Text>
            )}
            {preview.previewable && (
              <pre
                style={{
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  background: '#fafafa',
                  padding: 12,
                  borderRadius: 8,
                  margin: 0,
                  fontSize: 13,
                  lineHeight: 1.7,
                }}
              >
                {preview.text}
              </pre>
            )}
            {/* 超长截断提示：避免管理员误以为这就是全文 */}
            {preview.truncated && (
              <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
                （内容过长，仅显示前 {preview.text.length} 字符）
              </Typography.Text>
            )}
          </>
        ) : (
          <Typography.Text type="secondary">暂无预览内容</Typography.Text>
        )}
      </Drawer>

      {/* 驳回弹窗：理由必填（需求硬性要求，后端亦会校验） */}
      <Modal
        title={rejectTarget ? `驳回《${rejectTarget.filename}》` : '驳回'}
        open={rejectOpen}
        okText="确认驳回"
        cancelText="取消"
        okButtonProps={{ danger: true, loading: submitting }}
        onOk={() => void submitReject()}
        onCancel={() => setRejectOpen(false)}
        destroyOnClose
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
          请填写驳回理由，提交人将据此修改后重新提交。
        </Typography.Paragraph>
        <Input.TextArea
          rows={4}
          maxLength={500}
          showCount
          placeholder="例如：内容与知识库主题无关 / 缺少必要章节"
          value={rejectNote}
          onChange={(e) => setRejectNote(e.target.value)}
        />
      </Modal>
    </div>
  );
}
