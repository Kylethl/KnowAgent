import { useState } from 'react';
import { Button, Input, Modal, Radio, Space, Tooltip, message } from 'antd';
import { DislikeFilled, DislikeOutlined, LikeFilled, LikeOutlined } from '@ant-design/icons';
import type { FeedbackItem, FeedbackRating, FeedbackReason } from '../types';
import { deleteFeedback, submitFeedback } from '../services/feedbackApi';

// P6-F2：答案反馈按钮（点赞 / 点踩 + 点踩表单）
// ---------------------------------------------------------------------
// 契约来源：《需求清单》P6 功能2「答案反馈与评估」
//   「前端：回答点赞/点踩按钮 + 反馈表单」
//
// 交互设计（为什么这样定）：
//   1. 点赞：一次点击即提交，不弹表单——多一步输入就会流失大量正向信号；
//   2. 点踩：弹出表单收集归因（答非所问/内容有误/资料不符/其他）+ 可选说明。
//      归因是「后续优化」的关键输入：只知道「有人踩」，无法判断该改 prompt 还是改检索；
//   3. 再次点击同一按钮 = 取消反馈（用户点错了要能撤销，不必刷新页面）；
//   4. 先赞后踩：直接提交新评价，后端 upsert 覆盖，无需先删再建。
//
// 依赖的前置条件：messageId 必须是**后端真实消息 ID**。
//   流式回答在落库前没有 ID（后端下发 {"type":"message","id":...} 帧后才有），
//   因此 messageId 为空时本组件不渲染——宁可晚一点出现，也不要给出点了会 404 的按钮。

interface Props {
  // 后端消息 ID（messages.id）；为空表示该回答尚未落库，暂不提供反馈入口
  messageId?: string;
  // 初始反馈（历史回显 / 已评价状态），用于恢复按钮选中态
  initial?: FeedbackItem | null;
}

/** 点踩归因选项：与后端 feedback.reason 的 CHECK 约束一一对应。 */
const REASON_OPTIONS: { value: FeedbackReason; label: string }[] = [
  { value: 'off_topic', label: '答非所问' },
  { value: 'wrong', label: '内容有误' },
  { value: 'unsupported', label: '资料不符' },
  { value: 'other', label: '其他问题' },
];

// 补充说明长度上限：与后端 FEEDBACK_COMMENT_MAX_LEN 保持一致
const COMMENT_MAX_LEN = 1000;

export default function FeedbackButtons({ messageId, initial }: Props) {
  // 当前反馈（null 表示未评价）；本地状态优先，提交成功后立即更新
  const [feedback, setFeedback] = useState<FeedbackItem | null>(initial ?? null);
  // 是否有请求在飞：防止连点造成重复提交（后端虽幂等，但会浪费请求）
  const [submitting, setSubmitting] = useState(false);

  // 点踩表单弹窗状态
  const [modalOpen, setModalOpen] = useState(false);
  const [reason, setReason] = useState<FeedbackReason>('off_topic');
  const [comment, setComment] = useState('');

  // 没有后端 ID 就不渲染：此时提交必然失败（后端查不到该消息）
  if (!messageId) return null;

  const current: FeedbackRating | null = feedback?.rating ?? null;

  /**
   * 提交一个评价（新增或覆盖）。
   * @param rating 评价结论
   * @param extra 点踩时才有的归因与说明
   */
  const submit = async (
    rating: FeedbackRating,
    extra?: { reason?: FeedbackReason; comment?: string }
  ) => {
    setSubmitting(true);
    try {
      const saved = await submitFeedback(messageId, {
        rating,
        reason: rating === 'down' ? extra?.reason ?? null : null,
        comment: rating === 'down' ? (extra?.comment ?? '').trim() || null : null,
      });
      setFeedback(saved);
      // 正向反馈轻提示即可；点踩由弹窗关闭本身给出「已收到」的反馈感
      if (rating === 'up') message.success('感谢你的认可');
      setModalOpen(false);
    } catch (err) {
      message.error(`提交反馈失败：${(err as Error).message}`);
    } finally {
      setSubmitting(false);
    }
  };

  /**
   * 取消当前反馈（再次点击同一按钮时触发）。
   */
  const cancel = async () => {
    setSubmitting(true);
    try {
      await deleteFeedback(messageId);
      setFeedback(null);
      message.success('已取消评价');
    } catch (err) {
      message.error(`取消失败：${(err as Error).message}`);
    } finally {
      setSubmitting(false);
    }
  };

  /** 点赞按钮：已赞则取消，否则直接提交点赞。 */
  const handleLike = () => {
    if (submitting) return;
    if (current === 'up') {
      void cancel();
      return;
    }
    void submit('up');
  };

  /** 点踩按钮：已踩则取消，否则打开表单（默认归因「答非所问」）。 */
  const handleDislike = () => {
    if (submitting) return;
    if (current === 'down') {
      void cancel();
      return;
    }
    // 保留用户上次填过的内容（再次点开时不用重填）
    setReason((feedback?.reason as FeedbackReason) ?? 'off_topic');
    setComment(feedback?.comment ?? '');
    setModalOpen(true);
  };

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginTop: 4 }}>
        <span style={{ fontSize: 12, color: '#8c8c8c', marginRight: 2 }}>
          {feedback ? '感谢你的反馈' : '这个回答有帮助吗'}
        </span>

        {/* 点赞：选中态用实心图标 + 主色，未选中为线框灰 */}
        <Tooltip title={current === 'up' ? '取消点赞' : '有帮助'}>
          <Button
            type="text"
            size="small"
            loading={submitting && current === 'up'}
            icon={
              current === 'up' ? (
                <LikeFilled style={{ color: '#386BF3' }} />
              ) : (
                <LikeOutlined style={{ color: '#8c8c8c' }} />
              )
            }
            onClick={handleLike}
          />
        </Tooltip>

        {/* 点踩：点击后弹出归因表单 */}
        <Tooltip title={current === 'down' ? '取消评价' : '没帮助，反馈问题'}>
          <Button
            type="text"
            size="small"
            loading={submitting && current === 'down'}
            icon={
              current === 'down' ? (
                <DislikeFilled style={{ color: '#ff4d4f' }} />
              ) : (
                <DislikeOutlined style={{ color: '#8c8c8c' }} />
              )
            }
            onClick={handleDislike}
          />
        </Tooltip>

        {/* 已点踩：把原因以小字展示出来，让用户确认自己提交过什么 */}
        {current === 'down' && feedback?.reason && (
          <span style={{ fontSize: 12, color: '#8c8c8c' }}>
            （{REASON_OPTIONS.find((o) => o.value === feedback.reason)?.label ?? '已反馈'}）
          </span>
        )}
      </div>

      {/* 点踩表单：归因单选 + 可选补充说明 */}
      <Modal
        title="告诉我们哪里不好"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => void submit('down', { reason, comment })}
        okText="提交反馈"
        cancelText="取消"
        confirmLoading={submitting}
        destroyOnClose
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div>
            <div style={{ marginBottom: 8, fontSize: 13 }}>主要问题是什么？</div>
            <Radio.Group
              value={reason}
              onChange={(e) => setReason(e.target.value as FeedbackReason)}
            >
              <Space direction="vertical" size={6}>
                {REASON_OPTIONS.map((opt) => (
                  <Radio key={opt.value} value={opt.value}>
                    {opt.label}
                  </Radio>
                ))}
              </Space>
            </Radio.Group>
          </div>

          <div>
            <div style={{ marginBottom: 8, fontSize: 13 }}>
              补充说明（可选）
            </div>
            <Input.TextArea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="例如：答案引用的资料里没有这个结论"
              rows={3}
              maxLength={COMMENT_MAX_LEN}
              showCount
            />
          </div>
        </Space>
      </Modal>
    </>
  );
}
