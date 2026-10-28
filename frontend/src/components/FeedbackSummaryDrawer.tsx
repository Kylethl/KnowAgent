import { useCallback, useEffect, useState } from 'react';
import { Alert, Card, Col, Drawer, Empty, Row, Statistic, Tag, Typography, Spin, List } from 'antd';
import type { FeedbackSummary } from '../types';
import { FEEDBACK_REASON_LABELS, getFeedbackSummary } from '../services/feedbackApi';

// P6-F2：答案反馈评估面板（管理员）
// ---------------------------------------------------------------------
// 契约来源：《需求清单》P6 功能2「答案反馈与评估」
//   - 数据存储：feedback 表
//   - 后端：/messages/{id}/feedback 存储反馈
//   - AI Agent：反馈采集用于后续 prompt/检索离线优化
//
// 为什么管理员需要这个面板：
//   「有人点踩」本身不是结论，无法指导优化。这里把反馈变成可行动的数据：
//     1. 好评率：整体质量趋势（一眼看出是否在恶化）；
//     2. 归因分布：答非所问→改 prompt、内容有误→补知识/换模型、
//        资料不符→查检索与引用。对应不同的修复动作；
//     3. 最近差评明细：带「用户提问 + 回答摘要」，直接看到具体是哪个问题答坏了。
//
// 展示形态：右侧抽屉。管理员的主线任务是审核，评估是辅助视图，
// 不该占据常驻版面。

interface Props {
  open: boolean; // 抽屉是否展开
  onClose: () => void;
}

/** 把 0~1 的比例格式化为百分比文案（保留一位小数，避免 0/100 的粗糙感）。 */
function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

/** 归因代码 → 中文标签（复用 feedbackApi 中的统一映射）。 */
function reasonLabel(reason: string | null): string {
  if (!reason) return '未选择原因';
  return FEEDBACK_REASON_LABELS[reason] ?? reason;
}

export default function FeedbackSummaryDrawer({ open, onClose }: Props) {
  const [data, setData] = useState<FeedbackSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** 拉取评估概览（打开抽屉时调用）。 */
  const fetchSummary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await getFeedbackSummary(10));
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载反馈统计失败');
    } finally {
      setLoading(false);
    }
  }, []);

  // 每次打开都重新拉取：审核完一批文档后回来能看到最新数据
  useEffect(() => {
    if (open) void fetchSummary();
  }, [open, fetchSummary]);

  return (
    <Drawer title="答案反馈评估" placement="right" width={560} open={open} onClose={onClose}>
      {loading && !data ? (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <Spin />
        </div>
      ) : error ? (
        <Alert type="warning" showIcon message={error} />
      ) : !data || data.total === 0 ? (
        <Empty description="还没有用户反馈" style={{ padding: '40px 0' }} />
      ) : (
        <>
          {/* 概览：总数 / 好评率 / 赞 / 踩 四个数字，便于快速判断整体质量 */}
          <Row gutter={12} style={{ marginBottom: 16 }}>
            <Col span={6}>
              <Card size="small">
                <Statistic title="反馈总数" value={data.total} />
              </Card>
            </Col>
            <Col span={6}>
              <Card size="small">
                <Statistic title="好评率" value={formatRate(data.up_rate)} />
              </Card>
            </Col>
            <Col span={6}>
              <Card size="small">
                <Statistic title="点赞" value={data.up_count} valueStyle={{ color: '#386BF3' }} />
              </Card>
            </Col>
            <Col span={6}>
              <Card size="small">
                <Statistic title="点踩" value={data.down_count} valueStyle={{ color: '#ff4d4f' }} />
              </Card>
            </Col>
          </Row>

          {/* 归因分布：把「差评」拆成可修复的具体问题 */}
          <Typography.Title level={5} style={{ marginTop: 8 }}>
            点踩原因分布
          </Typography.Title>
          {Object.keys(data.reason_breakdown).length === 0 ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              点踩都没有填写原因，暂无法归因。
            </Typography.Text>
          ) : (
            <div style={{ marginBottom: 8 }}>
              {Object.entries(data.reason_breakdown).map(([reason, count]) => (
                <Tag key={reason} color="volcano" style={{ marginBottom: 6 }}>
                  {reasonLabel(reason)}：{count} 条
                </Tag>
              ))}
            </div>
          )}

          {/* 最近差评：带上下文，直接定位「哪个问题答坏了」 */}
          <Typography.Title level={5} style={{ marginTop: 16 }}>
            最近差评
          </Typography.Title>
          {data.recent_down.length === 0 ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              暂无差评记录。
            </Typography.Text>
          ) : (
            <List
              size="small"
              dataSource={data.recent_down}
              renderItem={(item) => (
                <List.Item style={{ display: 'block', padding: '10px 0' }}>
                  {/* 提问：管理员的定位锚点 */}
                  <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
                    Q：{item.question || '（找不到对应提问）'}
                  </div>
                  {/* 回答摘要：后端已截断到 300 字 */}
                  <Typography.Paragraph
                    type="secondary"
                    style={{ fontSize: 12, marginBottom: 4 }}
                    ellipsis={{ rows: 3 }}
                  >
                    A：{item.answer || '（回答已删除）'}
                  </Typography.Paragraph>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                    <Tag color="volcano">{reasonLabel(item.reason)}</Tag>
                    {item.comment && (
                      <Typography.Text style={{ fontSize: 12 }}>
                        用户说明：{item.comment}
                      </Typography.Text>
                    )}
                  </div>
                </List.Item>
              )}
            />
          )}

          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 16 }}>
            说明：统计的是「当前态度」——用户改赞或取消评价后，数字会随之变化，
            不代表历史操作流水。归因分布是后续 prompt 与检索优化的直接输入。
          </Typography.Paragraph>
        </>
      )}
    </Drawer>
  );
}
