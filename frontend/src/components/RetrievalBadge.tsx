import type { ReactNode } from 'react';
import { Tooltip } from 'antd';
import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  StopOutlined,
} from '@ant-design/icons';
import type { RetrievalInfo } from '../types';

// P4-F1：知识库检索状态徽标
// 职责：把后端下发的 retrieval 元信息渲染成助手气泡下方的可读状态，
//       让用户「看得见 RAG」——知道这条回答是否用了知识库、用了多少段资料。
//
// 与《需求清单》P4 功能1 的对应：
//   「前端：在 P1 对话页基础上接入检索增强渲染（引用区块先占位，见功能 2）」
//   本组件即该「占位区块」：功能 1 只展示检索状态与命中数，
//   引用卡片（标题/片段/可跳转）在 P4 功能2 中替换/扩展此区域。
//
// 样式约定：徽标整体低调（浅色底、小字号），不喧宾夺主，
//           与豆包式简洁风格一致；不同 mode 用不同配色区分语义。

// 每种 mode 的展示配置：文案、配色、图标
const MODE_STYLES: Record<
  RetrievalInfo['mode'],
  { text: string; color: string; bg: string; border: string; icon: ReactNode }
> = {
  // 正常命中：绿系，表示「有据可依」
  rag: {
    text: '知识库检索',
    color: '#0f7b3f',
    bg: '#eaf7ef',
    border: '#c6e9d4',
    icon: <CheckCircleOutlined />,
  },
  // 未命中：橙系，提示「这是通用回答」，避免用户误以为内容来自知识库
  no_hit: {
    text: '知识库未命中',
    color: '#ad6800',
    bg: '#fff7e6',
    border: '#ffe1a8',
    icon: <ExclamationCircleOutlined />,
  },
  // 未启用：灰系中性提示
  disabled: {
    text: '检索未启用',
    color: '#595959',
    bg: '#f5f5f5',
    border: '#e0e0e0',
    icon: <StopOutlined />,
  },
  // 降级：橙系，说明技术原因（hover 可见 detail）
  fallback: {
    text: '检索失败，已回退通用回答',
    color: '#ad4e00',
    bg: '#fff2e8',
    border: '#ffd6b3',
    icon: <ExclamationCircleOutlined />,
  },
};

/**
 * 生成徽标主文案：命中时带上片段数，其余 mode 用固定文案。
 * @param info 后端下发的检索元信息
 * @returns 展示文本
 */
function buildLabel(info: RetrievalInfo): string {
  const base = MODE_STYLES[info.mode];
  if (info.mode === 'rag') {
    // 命中数是最有价值的信息，直接拼进文案（如「知识库检索 · 命中 3 段资料」）
    return `${base.text} · 命中 ${info.count} 段资料`;
  }
  return base.text;
}

/**
 * 生成悬浮提示内容：解释模式含义 + 后端耗时/缓存情况 + 降级原因。
 * @param info 后端下发的检索元信息
 * @returns Tooltip 的文本内容
 */
function buildTooltip(info: RetrievalInfo): string {
  const lines: string[] = [];
  switch (info.mode) {
    case 'rag':
      // 措辞用「优先依据」而非「基于」：检索命中不等于模型一定采用，
      // 资料不足时模型仍可能判定无法回答（此时回答里会明确说明）。
      lines.push(`已从知识库检索到 ${info.count} 段相关资料，回答将优先依据这些资料生成。`);
      break;
    case 'no_hit':
      lines.push('未在知识库中找到与问题相关的资料，以下为通用回答。');
      break;
    case 'disabled':
      lines.push('当前未启用知识库检索，以下为通用回答。');
      break;
    case 'fallback':
      lines.push('检索过程出现异常，已回退为通用回答。');
      break;
  }
  // 后端附带的原因优先展示（对排查配置问题最有用，如「未配置可用的向量模型」）
  if (info.reason) {
    lines.push(`原因：${info.reason}`);
  }
  // 性能信息：命中缓存时明确标注，便于判断是否为缓存结果
  if (typeof info.elapsed_ms === 'number') {
    lines.push(`检索耗时：${info.elapsed_ms} ms${info.cached ? '（命中缓存）' : ''}`);
  }
  return lines.join('\n');
}

/**
 * 知识库检索状态徽标。
 * @param info 后端下发的检索元信息；为 undefined 时不渲染（如历史消息、或后端未下发该帧）
 */
export default function RetrievalBadge({ info }: { info?: RetrievalInfo }) {
  // 无检索信息：直接不渲染，保持历史消息与旧版界面一致
  if (!info) return null;

  const style = MODE_STYLES[info.mode];

  return (
    <Tooltip title={buildTooltip(info)} placement="top">
      <span
        className="retrieval-badge"
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 4,
          marginTop: 8,
          padding: '2px 8px',
          borderRadius: 10,
          fontSize: 12,
          lineHeight: '18px',
          color: style.color,
          background: style.bg,
          border: `1px solid ${style.border}`,
          cursor: 'default',
          userSelect: 'none',
        }}
      >
        {/* 图标按 mode 区分语义（成功/警示/禁用），不额外引入图标资源 */}
        {style.icon}
        <span>{buildLabel(info)}</span>
      </span>
    </Tooltip>
  );
}
