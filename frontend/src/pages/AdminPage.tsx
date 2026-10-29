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
  AudioOutlined,
  BarChartOutlined,
  CheckOutlined,
  CloseOutlined,
  EyeOutlined,
  PictureOutlined,
  ReloadOutlined,
  StopOutlined,
  FileTextOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import TopBar from '../components/TopBar';
import FeedbackSummaryDrawer from '../components/FeedbackSummaryDrawer';
import { statusLabel } from '../components/DocumentStatusTag';
import { useAuth } from '../hooks/useAuth';
import { adminDocumentMediaUrl, listAdminDocuments, previewDocument, reviewDocument } from '../services/adminApi';
import { adminReindexDocument, adminUnpublishDocument } from '../services/documentApi';
import type { AdminDocument, DocStatus, DocumentPreview, ReviewStatus } from '../types';
// 行级业务规则抽成纯函数（见 utils/adminRules.ts）：
// 「按钮是否可点」这类判定必须能被单测锁死——历史上正是因为
// `status === 'indexing'` 一律禁用，导致僵死文档只剩「预览」可用、永远无法重新入库。
import { canReindex, canUnpublish, isIndexingStuck, stuckMinutes } from '../utils/adminRules';

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
//
// P6-F2：标题区新增「反馈评估」入口，打开抽屉查看用户点赞/点踩的汇总
//        （好评率、点踩归因分布、最近差评明细），作为优化 prompt 与检索的依据。

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

/** 文件类型 → 展示文案与图标（P5 起区分图片/音频/视频）。 */
const TYPE_META: Record<string, { label: string; icon: JSX.Element }> = {
  word: { label: 'Word', icon: <FileTextOutlined /> },
  markdown: { label: 'Markdown', icon: <FileTextOutlined /> },
  image: { label: '图片', icon: <PictureOutlined /> },
  audio: { label: '音频', icon: <AudioOutlined /> },
  video: { label: '视频', icon: <VideoCameraOutlined /> },
};

/** 媒体类型集合：决定预览时用 <img>/<audio>/<video> 而非文本块。 */
const MEDIA_TYPES = ['image', 'audio', 'video'];

/** 把秒数格式化为 m:ss / h:mm:ss；未知返回 null。 */
function formatDuration(seconds?: number | null): string | null {
  if (!seconds || seconds <= 0) return null;
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

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

  // ---------- P6-F2：答案反馈评估抽屉 ----------
  const [feedbackOpen, setFeedbackOpen] = useState(false);

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
        width: 100,
        // P5：类型从 2 种扩展到 5 种，统一按映射表渲染（含图标，便于快速区分媒体）
        render: (t: string) => {
          const meta = TYPE_META[t] ?? { label: t, icon: <FileTextOutlined /> };
          return (
            <Space size={4}>
              {meta.icon}
              <span>{meta.label}</span>
            </Space>
          );
        },
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
          // 入库僵死（P5 修复）：上次入库被进程重启/worker 杀死，任务不会自行结束。
          // 必须显著区别于「入库中」，否则管理员会误以为「在跑，等着就好」——
          // 而实际上它永远不会完成，且该状态下「重新入库」本应可用。
          if (isIndexingStuck(row)) {
            // 分钟数取后端下发的已持续时长（前端不自行推算阈值）
            const minutes = stuckMinutes(row.indexing_elapsed_seconds) ?? 1;
            return (
              <Tooltip
                title={
                  `上次入库已中断：文档停留在「入库中」约 ${minutes} 分钟，超过僵死阈值，` +
                  '后台任务不会再自行结束。点击「重新入库」重跑即可恢复。'
                }
              >
                <Tag color="volcano">入库卡住</Tag>
              </Tooltip>
            );
          }
          const meta = STATUS_META[s] ?? { color: 'default', text: s };
          // P5：媒体类在入库阶段实际是 OCR/ASR 解析，文案显示「解析中」更贴合等待时长
          const text = statusLabel(s, row.file_type);
          // P4：向量过期提示（mock 向量 / 换过模型 / 历史未记录）。
          // 这类文档虽显示「已入库」，但检索结果不可信，需管理员点「重新入库」刷新向量，
          // 因此与状态标签并列展示，避免管理员误判为「已可用」。
          if (row.embedding_stale) {
            return (
              <span style={{ display: 'inline-flex', gap: 4 }}>
                <Tag color={meta.color}>{text}</Tag>
                <Tooltip
                  title={
                    `该文档的向量由「${row.embedding_model ?? '未知模型'}」生成，与当前向量模型不一致，` +
                    '检索结果不可靠。请点击「重新入库」重建向量。'
                  }
                >
                  <Tag color="orange" style={{ marginInlineEnd: 0 }}>
                    向量过期
                  </Tag>
                </Tooltip>
              </span>
            );
          }
          return <Tag color={meta.color}>{text}</Tag>;
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
            {/* 重新入库：需审核通过；入库中禁用，但**僵死必须放行**（判据见 canReindex）。 */}
            <Button
              size="small"
              icon={<ReloadOutlined />}
              disabled={!canReindex(row)}
              loading={indexingId === row.id}
              onClick={() => void handleReindex(row)}
            >
              重新入库
            </Button>
            {/* 下线：删除向量切片；正常入库中禁用（会与后台任务冲突），僵死时同样放行 */}
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
                disabled={!canUnpublish(row)}
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
      <TopBar
        user={user}
        onLogout={logout}
        onHomeClick={handleBackToChat}
        // P7：系统状态入口（监控 / 备份 / 审计 / 合规），仅管理员可见
        onOpsClick={() => navigate('/ops')}
      />


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
          <Space>
            {/* P6-F2：答案反馈评估（好评率 / 点踩归因 / 最近差评） */}
            <Button icon={<BarChartOutlined />} onClick={() => setFeedbackOpen(true)}>
              反馈评估
            </Button>
            <Button icon={<ArrowLeftOutlined />} onClick={handleBackToChat}>
              返回对话
            </Button>
          </Space>
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
            {/* P5：图片/音频/视频直接展示原文件（浏览器原生标签流式加载，不读进 JS 内存） */}
            {MEDIA_TYPES.includes(preview.file_type) && (
              <div style={{ marginBottom: 16 }}>
                {preview.file_type === 'image' && (
                  <img
                    src={adminDocumentMediaUrl(preview.id)}
                    alt={preview.filename}
                    style={{
                      maxWidth: '100%',
                      borderRadius: 8,
                      border: '1px solid #f0f0f0',
                      display: 'block',
                    }}
                  />
                )}
                {preview.file_type === 'audio' && (
                  <audio controls src={adminDocumentMediaUrl(preview.id)} style={{ width: '100%' }} />
                )}
                {preview.file_type === 'video' && (
                  <video
                    controls
                    src={adminDocumentMediaUrl(preview.id)}
                    style={{ width: '100%', borderRadius: 8, background: '#000' }}
                  />
                )}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  原文件可直接查看/播放；下方为 OCR/ASR 解析出的文本（入库后参与检索）。
                </Typography.Text>
              </div>
            )}

            {/* P5：解析元信息——让管理员判断「这一条是怎么被识别的、质量如何」 */}
            {preview.extract_meta && (
              <Space size={6} wrap style={{ marginBottom: 12 }}>
                {preview.extract_meta.model && (
                  <Tag color="purple">模型 {preview.extract_meta.model}</Tag>
                )}
                {formatDuration(preview.extract_meta.duration_seconds) && (
                  <Tag>时长 {formatDuration(preview.extract_meta.duration_seconds)}</Tag>
                )}
                {typeof preview.extract_meta.chars === 'number' && (
                  <Tag>识别 {preview.extract_meta.chars} 字</Tag>
                )}
                {typeof preview.extract_meta.elapsed_ms === 'number' && (
                  <Tag>耗时 {(preview.extract_meta.elapsed_ms / 1000).toFixed(1)}s</Tag>
                )}
                {typeof preview.extract_meta.segments === 'number' &&
                  preview.extract_meta.segments > 1 && <Tag>{preview.extract_meta.segments} 段转写</Tag>}
              </Space>
            )}

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

      {/* P6-F2：答案反馈评估抽屉（好评率 / 归因分布 / 最近差评明细） */}
      <FeedbackSummaryDrawer open={feedbackOpen} onClose={() => setFeedbackOpen(false)} />
    </div>
  );
}
