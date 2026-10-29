import { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  Descriptions,
  Empty,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { CloudDownloadOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { fetchBackups, pruneBackups, triggerBackup } from '../services/opsApi';
import type { BackupItem, BackupStatus } from '../types';

// P7：数据库备份面板（管理端「系统状态」页的一部分）
// ---------------------------------------------------------------------
// 契约来源：后端 GET /ops/backups、POST /ops/backups、POST /ops/backups/prune
//
// 设计要点：
//   1. **手动备份是同步执行**（大库可能数十秒），因此按钮必须 loading + 二次确认，
//      否则用户会以为没反应而反复点击（每次都真的跑一遍 pg_dump，浪费 IO）。
//   2. 备份文件是否 **verified** 展示在列表里：只显示「有备份」而不显示「备份可校验」
//      是运维最常见的安全错觉——文件存在不等于能恢复。
//   3. 恢复命令只做**展示与复制**，不提供一键恢复：恢复会覆盖整个数据库，
//      必须由人在服务器上显式执行（见 backend/scripts/restore_postgres.sh）。
//   4. 清理（prune）按保留策略删除，属于破坏性操作，用 Popconfirm 二次确认。

/** 把字节数格式化为可读文本。 */
function formatSize(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 把 ISO 时间格式化为「年-月-日 时:分」；空值返回占位符。 */
function formatTime(iso: string | null): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/** 触发方式 → 展示文案与配色。 */
const TRIGGER_META: Record<string, { text: string; color: string }> = {
  manual: { text: '手动', color: 'blue' },
  schedule: { text: '定时', color: 'green' },
  cli: { text: '脚本', color: 'purple' },
};

export default function BackupPanel() {
  // 备份策略与文件列表（后端一次返回，避免两次请求造成数据不一致）
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [items, setItems] = useState<BackupItem[]>([]);
  const [loading, setLoading] = useState(false);
  // 手动备份执行中：禁用按钮，避免并发触发多个 pg_dump
  const [running, setRunning] = useState(false);
  const [pruning, setPruning] = useState(false);

  /** 拉取备份列表与策略总览。 */
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchBackups(50);
      setStatus(data.status);
      setItems(data.items);
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载备份列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** 手动触发一次备份（同步等待后端执行完成）。 */
  const handleBackup = async () => {
    setRunning(true);
    try {
      const item = await triggerBackup();
      message.success(`备份完成：${item.name}（${formatSize(item.size_bytes)}）`);
      await load();
    } catch (err) {
      // 后端会回传可读原因（如「未找到 pg_dump，请安装 postgresql-client」）
      message.error(err instanceof Error ? err.message : '备份失败', 6);
    } finally {
      setRunning(false);
    }
  };

  /** 按保留策略清理过期备份。 */
  const handlePrune = async () => {
    setPruning(true);
    try {
      const result = await pruneBackups();
      message.success(
        result.removed > 0 ? `已清理 ${result.removed} 个过期备份` : '没有需要清理的备份',
      );
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '清理失败');
    } finally {
      setPruning(false);
    }
  };

  /** 复制恢复命令：比手抄文件名可靠，避免打错一个字就恢复错包。 */
  const copyRestoreHint = async (hint: string) => {
    try {
      await navigator.clipboard.writeText(hint);
      message.success('恢复命令已复制（请在服务器上人工执行）');
    } catch {
      // 非安全上下文（http）下 clipboard 不可用，降级提示用户手工复制
      message.warning('复制失败，请手动选中命令复制');
    }
  };

  const columns: ColumnsType<BackupItem> = [
    {
      title: '备份文件',
      dataIndex: 'name',
      ellipsis: true,
      render: (name: string) => <Typography.Text code>{name}</Typography.Text>,
    },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      width: 100,
      render: (v: number) => formatSize(v),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 160,
      render: (v: string) => formatTime(v),
    },
    {
      title: '触发',
      dataIndex: 'trigger',
      width: 100,
      render: (v: string | null) => {
        const meta = TRIGGER_META[v ?? ''] ?? { text: v ?? '—', color: 'default' };
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: '完整性',
      dataIndex: 'verified',
      width: 110,
      render: (verified: boolean, row) =>
        verified ? (
          // hover 展示 sha256：需要离线比对时可取用
          <Tooltip title={row.sha256 ? `sha256: ${row.sha256}` : '通过 gzip 完整性与 pg_dump 标记校验'}>
            <Tag color="green">已校验</Tag>
          </Tooltip>
        ) : (
          <Tooltip title="未通过完整性校验：该文件可能不完整，恢复前请先人工确认">
            <Tag color="orange">未校验</Tag>
          </Tooltip>
        ),
    },
    {
      title: '恢复',
      key: 'restore',
      width: 150,
      render: (_, row) => (
        <Button
          size="small"
          icon={<CloudDownloadOutlined />}
          onClick={() => void copyRestoreHint(row.restore_hint)}
        >
          复制恢复命令
        </Button>
      ),
    },
  ];

  return (
    <Card
      title="数据库备份"
      size="small"
      extra={
        <Space>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          <Popconfirm
            title="按保留策略清理过期备份？"
            description={`将按「${status?.retention_days ?? 0} 天 / ${status?.retention_count ?? 0} 份」删除超出部分，不可恢复。`}
            okText="清理"
            cancelText="取消"
            onConfirm={() => void handlePrune()}
          >
            <Button size="small" icon={<DeleteOutlined />} loading={pruning}>
              清理过期
            </Button>
          </Popconfirm>
          <Popconfirm
            title="立即执行一次数据库备份？"
            description="大库可能需要数十秒，执行期间请勿重复点击。"
            okText="开始备份"
            cancelText="取消"
            onConfirm={() => void handleBackup()}
          >
            <Button size="small" type="primary" loading={running}>
              立即备份
            </Button>
          </Popconfirm>
        </Space>
      }
    >
      {/* 策略概览：让运维一眼看到「备份开没开、存哪、留多久、下次什么时候跑」 */}
      <Descriptions size="small" column={2} style={{ marginBottom: 12 }}>
        <Descriptions.Item label="状态">
          {status?.enabled ? <Tag color="green">已启用</Tag> : <Tag color="orange">未启用</Tag>}
        </Descriptions.Item>
        <Descriptions.Item label="备份目录">
          <Typography.Text code>{status?.dir ?? '—'}</Typography.Text>
        </Descriptions.Item>
        <Descriptions.Item label="保留策略">
          {status ? `${status.retention_days} 天 / ${status.retention_count} 份` : '—'}
        </Descriptions.Item>
        <Descriptions.Item label="下次定时执行">
          {status?.next_run_hint ? formatTime(status.next_run_hint) : '—'}
          {status && status.enabled ? (
            <Typography.Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
              （每日 {String(status.schedule_hour).padStart(2, '0')}:00，由 Celery beat 触发）
            </Typography.Text>
          ) : null}
        </Descriptions.Item>
        <Descriptions.Item label="最近备份">
          {status?.last ? formatTime(status.last.created_at) : '尚无备份'}
        </Descriptions.Item>
        <Descriptions.Item label="占用空间">
          {status ? `${status.count} 份 / ${formatSize(status.total_bytes)}` : '—'}
        </Descriptions.Item>
      </Descriptions>

      {/* 无备份是一种「危险的空」：给出引导而不是空白表格 */}
      {items.length === 0 && !loading ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <span>
              尚无备份文件。生产环境请部署定时备份（Celery beat 或宿主机 crontab 调
              <Typography.Text code> scripts/backup_postgres.sh</Typography.Text>）。
            </span>
          }
        />
      ) : (
        <Table<BackupItem>
          rowKey="name"
          size="small"
          columns={columns}
          dataSource={items}
          loading={loading}
          pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 份` }}
        />
      )}

      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
        恢复必须人工执行：<Typography.Text code>bash scripts/restore_postgres.sh &lt;文件名&gt; --yes</Typography.Text>
        （脚本会先备份当前库、校验文件完整性，再以单事务恢复；不提供一键恢复接口，避免误操作覆盖生产库。）
      </Typography.Paragraph>
    </Card>
  );
}
