import { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  Input,
  Popover,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { fetchAuditActions, fetchAuditLogs } from '../services/opsApi';
import type { AuditActionOption, AuditLogItem } from '../types';

// P7：审计日志面板（管理端「系统状态」页的一部分）
// ---------------------------------------------------------------------
// 契约来源：《需求清单》P7 功能2「审计日志」
//   - 后端：GET /ops/audit-logs（多条件筛选 + 分页）、GET /ops/audit-logs/actions（动作字典）
//
// 查询行为说明：
//   1. 时间范围用「最近 24 小时 / 7 天 / 30 天 / 全部」快捷选项，而不是日期选择器——
//      运维排查通常是「刚才出问题那阵子」，选相对区间比选绝对日期快得多；
//   2. 所有筛选都由**后端**执行并分页，前端不做二次过滤：
//      审计表会随运行时间增长到百万级，前端过滤既慢又容易把「只看到一页」误当成全部；
//   3. 关键字搜索覆盖用户名 / 动作 / 资源 ID（后端用 ILIKE），适合「查某个文档被谁动过」。
//
// 安全提示：
//   后端对「查询审计」这个动作本身也会写一条审计（ops.audit.query），
//   因此**谁在调查谁**同样有记录——这也是合规审计的基本要求。

/** 结果筛选：'all' 为前端约定值，不传给后端。 */
type ResultFilter = 'all' | 'success' | 'failure';

/** 时间范围快捷选项：值为「最近多少小时」，0 表示不限。 */
type RangeOption = 24 | 168 | 720 | 0;

/** 把 ISO 时间格式化为「年-月-日 时:分:秒」（审计精确到秒才有意义）。 */
function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export default function AuditLogPanel() {
  // ---------- 筛选状态 ----------
  const [action, setAction] = useState<string | undefined>(undefined);
  const [result, setResult] = useState<ResultFilter>('all');
  const [keywordInput, setKeywordInput] = useState('');
  const [keyword, setKeyword] = useState('');
  const [range, setRange] = useState<RangeOption>(168); // 默认近 7 天：覆盖大多数排查场景

  // ---------- 数据状态 ----------
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const [actions, setActions] = useState<AuditActionOption[]>([]);

  /** 拉取动作字典（只需一次；失败时降级为「只能按结果/关键字筛选」）。 */
  useEffect(() => {
    let cancelled = false;
    fetchAuditActions()
      .then((list) => {
        if (!cancelled) setActions(list);
      })
      .catch(() => {
        // 字典加载失败不阻断主流程：下拉为空，其它筛选仍可用
        if (!cancelled) setActions([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /** 按当前筛选条件拉取一页审计记录。 */
  const load = useCallback(async () => {
    setLoading(true);
    try {
      // 把「最近 N 小时」换算成绝对时间：一次算出，避免前后两次请求的时间窗漂移
      const start =
        range > 0 ? new Date(Date.now() - range * 3600 * 1000).toISOString() : undefined;
      const data = await fetchAuditLogs({
        action,
        result: result === 'all' ? undefined : result,
        keyword: keyword || undefined,
        start,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      });
      setItems(data.items);
      setTotal(data.total);
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载审计日志失败');
    } finally {
      setLoading(false);
    }
  }, [action, result, keyword, range, page, pageSize]);

  useEffect(() => {
    void load();
  }, [load]);

  /** 条件变化后统一回到第一页（否则可能停在越界页码看到空列表）。 */
  const resetPage = () => setPage(1);

  const columns: ColumnsType<AuditLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 170,
      render: (v: string) => formatTime(v),
    },
    {
      title: '动作',
      dataIndex: 'action_label',
      width: 150,
      render: (label: string, row) => (
        // 展示中文名，hover 显示动作代码（排查时按代码 grep 日志更方便）
        <Tooltip title={row.action}>
          <span>{label}</span>
        </Tooltip>
      ),
    },
    {
      title: '操作者',
      dataIndex: 'username',
      width: 170,
      render: (name: string | null, row) => (
        <Space size={4}>
          <span>{name ?? '匿名'}</span>
          {row.role ? (
            <Tag color={row.role === 'admin' ? 'blue' : 'default'} style={{ marginInlineEnd: 0 }}>
              {row.role}
            </Tag>
          ) : null}
        </Space>
      ),
    },
    {
      title: '结果',
      dataIndex: 'result',
      width: 90,
      render: (v: string) =>
        v === 'success' ? <Tag color="green">成功</Tag> : <Tag color="red">失败</Tag>,
    },
    {
      title: '对象',
      key: 'resource',
      ellipsis: true,
      render: (_, row) =>
        row.resource_type ? (
          <Tooltip title={row.resource_id ?? ''}>
            <span>
              {row.resource_type}
              {row.resource_id ? `：${row.resource_id.slice(0, 8)}…` : ''}
            </span>
          </Tooltip>
        ) : (
          '—'
        ),
    },
    {
      title: '来源 IP',
      dataIndex: 'ip',
      width: 130,
      render: (v: string | null) => v ?? '—',
    },
    {
      title: '详情',
      key: 'detail',
      width: 90,
      render: (_, row) =>
        row.detail ? (
          <Popover
            title="动作详情（已脱敏）"
            content={
              <pre style={{ margin: 0, maxWidth: 420, maxHeight: 260, overflow: 'auto', fontSize: 12 }}>
                {JSON.stringify(row.detail, null, 2)}
              </pre>
            }
          >
            <Button size="small" type="link">
              查看
            </Button>
          </Popover>
        ) : (
          '—'
        ),
    },
  ];

  return (
    <Card
      title="审计日志"
      size="small"
      extra={
        <Space wrap>
          <Select
            size="small"
            allowClear
            placeholder="全部动作"
            style={{ width: 170 }}
            value={action}
            onChange={(value) => {
              setAction(value);
              resetPage();
            }}
            options={actions.map((item) => ({ label: item.label, value: item.value }))}
            showSearch
            optionFilterProp="label"
          />
          <Segmented<ResultFilter>
            size="small"
            value={result}
            onChange={(value) => {
              setResult(value);
              resetPage();
            }}
            options={[
              { label: '全部', value: 'all' },
              { label: '成功', value: 'success' },
              { label: '失败', value: 'failure' },
            ]}
          />
          <Segmented<RangeOption>
            size="small"
            value={range}
            onChange={(value) => {
              setRange(value);
              resetPage();
            }}
            options={[
              { label: '24 小时', value: 24 },
              { label: '7 天', value: 168 },
              { label: '30 天', value: 720 },
              { label: '全部', value: 0 },
            ]}
          />
          <Input.Search
            size="small"
            allowClear
            placeholder="用户 / 动作 / 资源 ID"
            style={{ width: 200 }}
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            onSearch={(value) => {
              setKeyword(value.trim());
              resetPage();
            }}
          />
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
            刷新
          </Button>
        </Space>
      }
    >
      <Table<AuditLogItem>
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={items}
        loading={loading}
        scroll={{ x: 900 }}
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
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
        查询审计本身也会被记录（ops.audit.query）：谁能看到审计，本身也受审计。
      </Typography.Paragraph>
    </Card>
  );
}
