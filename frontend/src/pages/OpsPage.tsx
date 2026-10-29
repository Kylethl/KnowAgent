import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Progress,
  Row,
  Segmented,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  ApiOutlined,
  ArrowLeftOutlined,
  CloudServerOutlined,
  DashboardOutlined,
  LineChartOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import TopBar from '../components/TopBar';
import AuditLogPanel from '../components/AuditLogPanel';
import BackupPanel from '../components/BackupPanel';
import { useAuth } from '../hooks/useAuth';
import { fetchCompliance, fetchOpsStatus } from '../services/opsApi';
import type {
  ComplianceItem,
  ComplianceReport,
  OpsResidencyTarget,
  OpsServiceStatus,
  OpsStatus,
  ServiceStatus,
} from '../types';

// P7：系统状态页（管理端）
// ---------------------------------------------------------------------
// 契约来源：《需求清单》P7 功能1「监控 / 日志 / 备份」与功能2「合规与私有化部署」
//   - 前端：基础状态页（需求标注为「可选」，这里做成**可运维**的完整面板）
//   - 后端：GET /ops/status、/ops/compliance、/ops/audit-logs、/ops/backups
//
// 页面结构（从上到下 = 从「有没有问题」到「具体是什么」）：
//   1. 顶部横幅：只在有异常时出现（依赖不可用 / 合规 fail），避免长期占据视觉焦点；
//   2. 四张概览卡：运行状态、依赖健康、今日审计、最近备份——一屏回答「现在能不能用」；
//   3. 依赖健康表：每项的状态、说明、探测耗时（失败原因直接写在这里）；
//   4. LLM 限流与配额：配置值 + 当前管理员本人用量（配额按人计，展示别人的没意义）；
//   5. 备份面板（独立组件，自带数据加载与操作）；
//   6. 数据不出域：策略 + 各能力实际出站目标（合规检查的核心证据）；
//   7. 合规自检：分组清单，pass/warn/fail + 修复建议；
//   8. 审计日志（独立组件，自带筛选与分页）。
//
// 刷新策略：
//   - 默认 30 秒自动刷新（运维页面挂在副屏上时能持续反映现状）；
//   - 可关闭（排查历史问题时不想被刷新打断）；
//   - 页面隐藏时不刷新（省流量，也避免后台标签页堆积请求）。
//
// 权限：进入本页前已由 <RequireAdmin> 保证 role='admin'；
//      真正的裁决仍在后端 require_admin（前端守卫只是体验优化）。

/** 依赖状态 → 标签文案与配色（与后端 ServiceStatus 取值一一对应）。 */
const SERVICE_META: Record<ServiceStatus, { text: string; color: string }> = {
  ok: { text: '正常', color: 'green' },
  degraded: { text: '降级可用', color: 'orange' },
  down: { text: '不可用', color: 'red' },
  disabled: { text: '未启用', color: 'default' },
};

/** 合规项状态 → 标签文案与配色。 */
const COMPLIANCE_META: Record<string, { text: string; color: string }> = {
  pass: { text: '通过', color: 'green' },
  warn: { text: '需关注', color: 'orange' },
  fail: { text: '不通过', color: 'red' },
};

/** 把秒数格式化为「N 天 N 小时 N 分」；不足一分钟显示秒。 */
function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分`;
}

/** 把 ISO 时间格式化为「年-月-日 时:分」。 */
function formatTime(iso: string | null): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

export default function OpsPage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  // ---------- 数据状态 ----------
  const [status, setStatus] = useState<OpsStatus | null>(null);
  const [compliance, setCompliance] = useState<ComplianceReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  // 自动刷新开关（默认开）：运维页面本质是「常驻看板」
  const [autoRefresh, setAutoRefresh] = useState(true);

  /**
   * 拉取状态与合规报告。
   * @param silent 静默刷新（不显示整页 loading，避免看板每隔 30 秒闪一下）
   */
  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      // 两个请求并发：合规自检内部还要查 Redis，串行会让页面慢一倍
      const [statusData, complianceData] = await Promise.all([
        fetchOpsStatus(),
        fetchCompliance(),
      ]);
      setStatus(statusData);
      setCompliance(complianceData);
      setError(null);
      setLastUpdated(new Date());
    } catch (err) {
      // 状态页本身失败时保留上一次数据（比整页空白更有用），只提示错误
      setError(err instanceof Error ? err.message : '加载系统状态失败');
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  // 首次进入立即加载
  useEffect(() => {
    void load();
  }, [load]);

  // 自动刷新：30 秒一轮；页面隐藏时跳过（后台标签页的定时器本来也会被节流）
  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        void load(true);
      }
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, load]);

  // ---------- 派生数据（用 useMemo 避免每次渲染重复计算） ----------

  /** 不健康的依赖（down/degraded），用于顶部横幅与概览卡。 */
  const unhealthy = useMemo(
    () => (status?.services ?? []).filter((s) => s.status === 'down' || s.status === 'degraded'),
    [status],
  );

  /** 依赖健康比例（只统计已启用的依赖，未启用的不算「不健康」）。 */
  const healthStats = useMemo(() => {
    const list = status?.services ?? [];
    const active = list.filter((s) => s.status !== 'disabled');
    const ok = active.filter((s) => s.status === 'ok').length;
    return { ok, total: active.length, all: list.length };
  }, [status]);

  /** 合规失败项（fail）：只在横幅里提示，具体内容在下方清单。 */
  const complianceFails = useMemo(
    () => (compliance?.items ?? []).filter((item) => item.status === 'fail'),
    [compliance],
  );

  /** 合规项按分类分组（类别顺序按首次出现，保持后端定义顺序）。 */
  const complianceGroups = useMemo(() => {
    const groups = new Map<string, ComplianceItem[]>();
    for (const item of compliance?.items ?? []) {
      const list = groups.get(item.category) ?? [];
      list.push(item);
      groups.set(item.category, list);
    }
    return Array.from(groups.entries());
  }, [compliance]);

  // ---------- 表格列定义 ----------

  const serviceColumns: ColumnsType<OpsServiceStatus> = [
    {
      title: '依赖',
      dataIndex: 'label',
      width: 200,
      render: (label: string, row) => (
        <Space size={6}>
          <ApiOutlined style={{ color: row.status === 'ok' ? '#52c41a' : '#bfbfbf' }} />
          <span>{label}</span>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (value: ServiceStatus) => {
        const meta = SERVICE_META[value];
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: '说明',
      dataIndex: 'detail',
      ellipsis: true,
      // 失败原因可能很长（异常栈信息），hover 展示全文
      render: (detail: string) => <Tooltip title={detail}>{detail || '—'}</Tooltip>,
    },
    {
      title: '耗时',
      dataIndex: 'latency_ms',
      width: 100,
      render: (value: number | null) => (value === null ? '—' : `${value.toFixed(0)} ms`),
    },
  ];

  const residencyColumns: ColumnsType<OpsResidencyTarget> = [
    { title: '用途', dataIndex: 'label', width: 180 },
    {
      title: '目标域名',
      dataIndex: 'host',
      ellipsis: true,
      render: (host: string, row) => (
        <Tooltip title={row.url}>
          <Typography.Text code>{host || '—'}</Typography.Text>
        </Tooltip>
      ),
    },
    {
      title: '模型',
      dataIndex: 'model',
      ellipsis: true,
    },
    {
      title: '部署位置',
      dataIndex: 'local',
      width: 120,
      render: (local: boolean) =>
        local ? <Tag color="cyan">本机 / 内网</Tag> : <Tag>外部云服务</Tag>,
    },
    {
      title: '当前策略',
      dataIndex: 'allowed',
      width: 110,
      render: (allowed: boolean) =>
        allowed ? <Tag color="green">允许出站</Tag> : <Tag color="red">已拦截</Tag>,
    },
  ];

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column', background: '#f7f8fa' }}>
      {/* 顶栏：复用对话页顶栏，提供返回对话 / 管理后台的入口 */}
      <TopBar
        user={user}
        onLogout={logout}
        onHomeClick={() => navigate('/')}
        onAdminClick={() => navigate('/admin')}
      />

      <main style={{ flex: 1, padding: 24, maxWidth: 1280, width: '100%', margin: '0 auto' }}>
        {/* ---------- 标题区 ---------- */}
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
              系统状态
            </Typography.Title>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              依赖健康 · LLM 配额 · 备份 · 审计 · 合规自检
              {lastUpdated ? ` ｜ 最近更新 ${formatTime(lastUpdated.toISOString())}` : ''}
            </Typography.Text>
          </div>
          <Space wrap>
            <Segmented<boolean>
              value={autoRefresh}
              onChange={(value) => setAutoRefresh(value)}
              options={[
                { label: '自动刷新 30s', value: true },
                { label: '手动刷新', value: false },
              ]}
            />
            <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
            {status?.metrics.grafana_url ? (
              <Button
                icon={<LineChartOutlined />}
                href={status.metrics.grafana_url}
                target="_blank"
                rel="noreferrer"
              >
                监控面板
              </Button>
            ) : null}
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/admin')}>
              返回后台
            </Button>
          </Space>
        </div>

        {/* ---------- 异常横幅（只在有问题时出现） ---------- */}
        {error ? (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="状态加载失败"
            description={`${error}（页面展示的是上一次成功获取的数据）`}
          />
        ) : null}
        {unhealthy.length > 0 ? (
          <Alert
            type={unhealthy.some((s) => s.status === 'down') ? 'error' : 'warning'}
            showIcon
            style={{ marginBottom: 16 }}
            message={`${unhealthy.length} 项依赖未处于正常状态`}
            description={unhealthy.map((s) => `${s.label}（${SERVICE_META[s.status].text}）`).join('、')}
          />
        ) : null}
        {complianceFails.length > 0 ? (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message={`合规自检有 ${complianceFails.length} 项不通过`}
            description={complianceFails.map((item) => item.title).join('、') + '，详见下方合规自检。'}
          />
        ) : null}

        {/* ---------- 概览卡 ---------- */}
        <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
          <Col xs={24} sm={12} lg={6}>
            <Card size="small" loading={loading && !status}>
              <Statistic
                title="运行状态"
                value={status?.app.env === 'production' ? '生产环境' : '开发环境'}
                prefix={
                  <Badge
                    status={unhealthy.some((s) => s.status === 'down') ? 'error' : 'success'}
                    style={{ marginRight: 6 }}
                  />
                }
                valueStyle={{ fontSize: 20 }}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {status ? `v${status.app.version} · 已运行 ${formatUptime(status.app.uptime_seconds)}` : '—'}
              </Typography.Text>
            </Card>
          </Col>
          <Col xs={24} sm={12} lg={6}>
            <Card size="small" loading={loading && !status}>
              <Statistic
                title="依赖健康"
                value={`${healthStats.ok}/${healthStats.total}`}
                prefix={<CloudServerOutlined />}
                valueStyle={{
                  fontSize: 20,
                  color: healthStats.ok === healthStats.total ? '#3f8600' : '#cf1322',
                }}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {unhealthy.length === 0 ? '全部依赖正常' : `${unhealthy.length} 项异常`}
              </Typography.Text>
            </Card>
          </Col>
          <Col xs={24} sm={12} lg={6}>
            <Card size="small" loading={loading && !status}>
              <Statistic
                title="今日审计事件"
                value={status?.audit.today ?? 0}
                prefix={<SafetyCertificateOutlined />}
                valueStyle={{ fontSize: 20 }}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                近 {status?.audit.window_days ?? 7} 天失败 {status?.audit.failures ?? 0} 次
              </Typography.Text>
            </Card>
          </Col>
          <Col xs={24} sm={12} lg={6}>
            <Card size="small" loading={loading && !status}>
              <Statistic
                title="最近备份"
                value={status?.backup.last ? formatTime(status.backup.last.created_at) : '尚无备份'}
                prefix={<DashboardOutlined />}
                valueStyle={{ fontSize: 18 }}
              />
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12 }}
              >
                {status?.backup.enabled
                  ? status.backup.last
                    ? `${status.backup.count} 份 · ${status.backup.last.verified ? '已校验' : '未校验'}`
                    : '备份已启用，等待首次执行'
                  : '备份未启用'}
              </Typography.Text>
            </Card>
          </Col>
        </Row>

        {/* ---------- 依赖健康 ---------- */}
        <Card
          size="small"
          title="依赖健康"
          style={{ marginBottom: 16 }}
          extra={
            <Space size={12}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                日志 {status?.app.log_format ?? '—'} / {status?.app.log_level ?? '—'}
              </Typography.Text>
              <Tooltip
                title={
                  status?.app.sentry_enabled
                    ? '错误上报已启用（Sentry）'
                    : '错误上报未启用（未配置 SENTRY_DSN，不会外发任何数据）'
                }
              >
                <Tag color={status?.app.sentry_enabled ? 'green' : 'default'}>
                  Sentry {status?.app.sentry_enabled ? '开' : '关'}
                </Tag>
              </Tooltip>
              <Tooltip title={`指标后端：${status?.app.metrics_backend ?? '—'}`}>
                <Tag color={status?.app.metrics_enabled ? 'blue' : 'default'}>
                  指标 {status?.app.metrics_enabled ? '开' : '关'}
                </Tag>
              </Tooltip>
            </Space>
          }
        >
          <Table<OpsServiceStatus>
            rowKey="name"
            size="small"
            columns={serviceColumns}
            dataSource={status?.services ?? []}
            pagination={false}
            loading={loading && !status}
          />
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '8px 0 0' }}>
            探测均为只读且带 2 秒超时；「降级可用」表示功能仍可用但能力受限（如 Redis 不可用时缓存退化）。
            指标抓取路径：<Typography.Text code>{status?.metrics.path ?? '/metrics'}</Typography.Text>
            {status?.metrics.token_required ? '（已启用抓取令牌）' : ''}
          </Typography.Paragraph>
        </Card>

        {/* ---------- LLM 限流与配额 ---------- */}
        <Card size="small" title="LLM 限流与配额" style={{ marginBottom: 16 }}>
          {status ? (
            <Row gutter={[16, 16]}>
              <Col xs={24} lg={10}>
                <Descriptions size="small" column={1}>
                  <Descriptions.Item label="对话模型">
                    {status.llm.configured ? (
                      <Tag color="green">{status.llm.model}</Tag>
                    ) : (
                      <Tag color="red">未配置 Key</Tag>
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label="限流开关">
                    {status.llm.rate_limit_enabled ? (
                      <Tag color="blue">已启用</Tag>
                    ) : (
                      <Tag color="orange">已关闭（成本不受控）</Tag>
                    )}
                  </Descriptions.Item>
                  <Descriptions.Item label="限额">
                    每分钟 {status.llm.rate_limit_per_minute} 次 · 每日{' '}
                    {status.llm.rate_limit_per_day} 次 · 每日 token{' '}
                    {status.llm.daily_token_quota > 0 ? status.llm.daily_token_quota : '不限'}
                  </Descriptions.Item>
                  <Descriptions.Item label="计数来源">
                    <Tooltip title="redis=跨副本精确计数；memory=进程内计数（单副本或 Redis 不可用时的降级）">
                      <Tag color={status.llm.quota?.source === 'redis' ? 'green' : 'default'}>
                        {status.llm.quota?.source ?? '—'}
                      </Tag>
                    </Tooltip>
                  </Descriptions.Item>
                </Descriptions>
              </Col>
              <Col xs={24} lg={14}>
                {status.llm.quota ? (
                  <Space direction="vertical" style={{ width: '100%' }} size={10}>
                    <div>
                      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                        <Typography.Text style={{ fontSize: 12 }}>
                          本分钟用量（当前账号）
                        </Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {status.llm.quota.minute_used} / {status.llm.quota.minute_limit}
                        </Typography.Text>
                      </Space>
                      <Progress
                        percent={Math.min(
                          100,
                          Math.round(
                            (status.llm.quota.minute_used /
                              Math.max(1, status.llm.quota.minute_limit)) *
                              100,
                          ),
                        )}
                        size="small"
                        status={status.llm.quota.minute_used >= status.llm.quota.minute_limit ? 'exception' : 'active'}
                      />
                    </div>
                    <div>
                      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                        <Typography.Text style={{ fontSize: 12 }}>今日次数</Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {status.llm.quota.day_used} / {status.llm.quota.day_limit}
                        </Typography.Text>
                      </Space>
                      <Progress
                        percent={Math.min(
                          100,
                          Math.round(
                            (status.llm.quota.day_used / Math.max(1, status.llm.quota.day_limit)) *
                              100,
                          ),
                        )}
                        size="small"
                      />
                    </div>
                    <div>
                      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                        <Typography.Text style={{ fontSize: 12 }}>今日 token</Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {status.llm.quota.tokens_used} /{' '}
                          {status.llm.quota.tokens_quota > 0 ? status.llm.quota.tokens_quota : '不限'}
                        </Typography.Text>
                      </Space>
                      <Progress
                        percent={
                          status.llm.quota.tokens_quota > 0
                            ? Math.min(
                                100,
                                Math.round(
                                  (status.llm.quota.tokens_used / status.llm.quota.tokens_quota) *
                                    100,
                                ),
                              )
                            : 0
                        }
                        size="small"
                      />
                    </div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      <SyncOutlined spin={autoRefresh} style={{ marginRight: 6 }} />
                      配额按用户计算，此处展示的是当前登录账号；全局用量请看 Grafana
                      （knowagent_llm_tokens_total）。
                    </Typography.Text>
                  </Space>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无配额信息" />
                )}
              </Col>
            </Row>
          ) : (
            <Typography.Text type="secondary">加载中…</Typography.Text>
          )}
        </Card>

        {/* ---------- 备份 ---------- */}
        <div style={{ marginBottom: 16 }}>
          <BackupPanel />
        </div>

        {/* ---------- 数据不出域 ---------- */}
        <Card size="small" title="数据不出域（出站目标）" style={{ marginBottom: 16 }}>
          <Descriptions size="small" column={3} style={{ marginBottom: 8 }}>
            <Descriptions.Item label="策略模式">
              {status?.residency.strict ? (
                <Tag color="green">strict（强制白名单）</Tag>
              ) : (
                <Tag color="orange">standard（仅记录）</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="数据区域">{status?.residency.region ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="白名单">
              {status?.residency.allowlist.length
                ? status.residency.allowlist.map((item) => (
                    <Tag key={item} color="blue">
                      {item}
                    </Tag>
                  ))
                : '仅允许本机 / 内网'}
            </Descriptions.Item>
          </Descriptions>
          <Table<OpsResidencyTarget>
            rowKey="purpose"
            size="small"
            columns={residencyColumns}
            dataSource={status?.residency.targets ?? []}
            pagination={false}
          />
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '8px 0 0' }}>
            私有化部署时把模型地址指向内网网关（如 vLLM / 本地 DeepSeek），并设
            <Typography.Text code> DATA_RESIDENCY_MODE=strict</Typography.Text>
            ：此时任何不在白名单内的外发请求都会被拒绝（503），而不是悄悄发出去。
          </Typography.Paragraph>
        </Card>

        {/* ---------- 合规自检 ---------- */}
        <Card
          size="small"
          title="合规自检"
          style={{ marginBottom: 16 }}
          extra={
            compliance ? (
              <Space size={8}>
                <Badge color="#52c41a" text={`通过 ${compliance.summary.pass ?? 0}`} />
                <Badge color="#faad14" text={`关注 ${compliance.summary.warn ?? 0}`} />
                <Badge color="#ff4d4f" text={`不通过 ${compliance.summary.fail ?? 0}`} />
              </Space>
            ) : null
          }
        >
          {complianceGroups.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可检查项" />
          ) : (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {complianceGroups.map(([category, list]) => (
                <div key={category}>
                  <Typography.Text strong style={{ fontSize: 13 }}>
                    {category}
                  </Typography.Text>
                  <div style={{ marginTop: 6 }}>
                    {list.map((item) => {
                      const meta = COMPLIANCE_META[item.status] ?? { text: item.status, color: 'default' };
                      return (
                        <div
                          key={item.key}
                          style={{
                            display: 'flex',
                            gap: 8,
                            alignItems: 'flex-start',
                            padding: '6px 0',
                            borderBottom: '1px dashed #f0f0f0',
                          }}
                        >
                          <Tag color={meta.color} style={{ minWidth: 58, textAlign: 'center', marginInlineEnd: 0 }}>
                            {meta.text}
                          </Tag>
                          <div style={{ flex: 1 }}>
                            <Typography.Text style={{ fontSize: 13 }}>{item.title}</Typography.Text>
                            <div>
                              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                                {item.detail}
                              </Typography.Text>
                            </div>
                            {item.hint && item.status !== 'pass' ? (
                              <div>
                                <Typography.Text type="warning" style={{ fontSize: 12 }}>
                                  处理建议：{item.hint}
                                </Typography.Text>
                              </div>
                            ) : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </Space>
          )}
        </Card>

        {/* ---------- 审计日志 ---------- */}
        <AuditLogPanel />
      </main>
    </div>
  );
}
