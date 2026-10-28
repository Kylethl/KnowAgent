import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Drawer, Empty, Skeleton, Space, Tag, Tooltip, Typography } from 'antd';
import {
  FileMarkdownOutlined,
  FileTextOutlined,
  FileWordOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons';
import type { ChunkDetail, Citation } from '../types';
import { getChunkDetail } from '../services/documentApi';

// P4-F2：答案引用来源卡片
// ---------------------------------------------------------------------
// 对应《需求清单》P4 功能2：
//   「前端：回答下方展示引用文档卡片（标题、片段、可跳转）」
//
// 交互设计（兼顾信息密度与不打扰）：
//   1. 卡片常驻在回答气泡下方，一行展示「序号 + 文档标题 + 章节 + 相关度」；
//      片段默认只显示 2 行，hover 可看全文摘要——回答是主体，引用不该喧宾夺主。
//   2. 点击卡片 → 右侧抽屉展示该片段**完整正文**与所在章节，并支持
//      「上一段 / 下一段」浏览上下文（数据来自 GET /documents/{id}/chunks/{chunk_id}）。
//      这就是需求里的「可跳转」：知识库是共享的，但不开放原始文件下载，
//      所以跳转的落点是「原文片段」而不是「文件」。
//   3. 只有被回答直接引用的卡片（cited=true）才带序号徽标；
//      未标注引用的（模型没按约定标注时的回退展示）用灰色「参考」标签区分，
//      避免用户误以为回答里出现过这个编号。

// 文档类型 → 图标与颜色（与上传面板保持一致，降低认知成本）
const TYPE_ICON: Record<string, { icon: JSX.Element; color: string }> = {
  word: { icon: <FileWordOutlined />, color: '#2b579a' },
  markdown: { icon: <FileMarkdownOutlined />, color: '#5191d1' },
};

/** 取文档类型对应的图标（未知类型回退为通用文档图标）。 */
function typeIcon(fileType: string) {
  const meta = TYPE_ICON[fileType] ?? { icon: <FileTextOutlined />, color: '#8c8c8c' };
  return <span style={{ color: meta.color }}>{meta.icon}</span>;
}

/**
 * 引用卡片列表。
 * @param citations 引用来源列表；为空时不渲染（如未启用检索、旧历史消息）
 */
export default function CitationList({ citations }: { citations?: Citation[] }) {
  // 当前打开的片段（null 表示抽屉关闭）
  const [active, setActive] = useState<Citation | null>(null);

  if (!citations || citations.length === 0) return null;

  return (
    <>
      <div
        style={{
          marginTop: 10,
          paddingTop: 10,
          // 与气泡之间用细分割线区隔，视觉上明确「这是回答的附注」
          borderTop: '1px dashed #e5e6eb',
        }}
      >
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          引用来源（{citations.length}）
        </Typography.Text>

        <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {citations.map((c) => (
            <CitationCard key={`${c.doc_id}-${c.chunk_id}-${c.index}`} citation={c} onOpen={setActive} />
          ))}
        </div>
      </div>

      {/* 片段详情抽屉：点击卡片后展示完整正文与相邻片段 */}
      <CitationDetailDrawer citation={active} onClose={() => setActive(null)} />
    </>
  );
}

/**
 * 单张引用卡片。
 * @param citation 引用数据
 * @param onOpen 点击回调（由父组件打开详情抽屉）
 */
function CitationCard({
  citation,
  onOpen,
}: {
  citation: Citation;
  onOpen: (c: Citation) => void;
}) {
  // 相关度展示用语义相似度（0~1），比综合排序分更直观
  const relevance = Math.round((citation.vector_score || citation.score || 0) * 100);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(citation)}
      onKeyDown={(e) => {
        // 键盘可达性：Enter / 空格等效于点击
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen(citation);
        }
      }}
      style={{
        display: 'flex',
        gap: 8,
        padding: '8px 10px',
        border: '1px solid #eef0f3',
        borderRadius: 8,
        background: '#fff',
        cursor: 'pointer',
        // hover 反馈由 CSS 类提供（见 index.css），避免为每张卡片维护 state
        transition: 'border-color .2s, box-shadow .2s',
      }}
      className="citation-card"
    >
      {/* 序号徽标：与回答正文里的 [n] 对应；未直接引用时显示「参考」 */}
      {citation.cited ? (
        <span
          style={{
            flex: '0 0 auto',
            width: 20,
            height: 20,
            lineHeight: '20px',
            textAlign: 'center',
            borderRadius: 4,
            background: '#eef3ff',
            color: '#386BF3',
            fontSize: 12,
            fontWeight: 600,
          }}
        >
          {citation.index}
        </span>
      ) : (
        <Tag style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: '18px' }}>参考</Tag>
      )}

      <div style={{ flex: 1, minWidth: 0 }}>
        {/* 第一行：类型图标 + 文档标题 + 章节 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
          {typeIcon(citation.file_type)}
          <Tooltip title={citation.doc_title}>
            <span
              style={{
                maxWidth: 220,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontSize: 13,
                color: '#1f1f1f',
              }}
            >
              {citation.doc_title}
            </span>
          </Tooltip>
          {citation.heading && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              · {citation.heading}
            </Typography.Text>
          )}
        </div>

        {/* 第二行：片段摘要，最多两行，超出省略（全文在抽屉里看） */}
        <div
          style={{
            marginTop: 2,
            fontSize: 12,
            color: '#646a73',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
            wordBreak: 'break-word',
          }}
        >
          {citation.snippet}
        </div>
      </div>

      {/* 右侧：相关度（悬停说明含义，避免用户把百分比当"准确率"） */}
      <Tooltip title="语义相似度（检索时该片段与问题的余弦相似度）">
        <span style={{ flex: '0 0 auto', fontSize: 12, color: '#8f959e' }}>{relevance}%</span>
      </Tooltip>
    </div>
  );
}

/**
 * 引用片段详情抽屉（「可跳转」的落点）。
 *
 * @param citation 当前查看的引用；为 null 时抽屉关闭
 * @param onClose 关闭回调
 */
function CitationDetailDrawer({
  citation,
  onClose,
}: {
  citation: Citation | null;
  onClose: () => void;
}) {
  // 当前展示的片段（打开后可通过上一段/下一段切换，因此与 citation 分开保存）
  const [detail, setDetail] = useState<ChunkDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * 拉取指定切片的详情。
   * @param docId 文档 ID
   * @param chunkId 切片 ID
   */
  const load = useCallback(async (docId: string, chunkId: string) => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await getChunkDetail(docId, chunkId));
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载引用片段失败');
      setDetail(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // 打开新引用时加载对应片段；关闭时清空，避免下次打开闪现上一篇内容
  useEffect(() => {
    if (!citation) {
      setDetail(null);
      setError(null);
      return;
    }
    void load(citation.doc_id, citation.chunk_id);
  }, [citation, load]);

  return (
    <Drawer
      title={detail ? detail.doc_title : citation?.doc_title ?? '引用原文'}
      placement="right"
      width={560}
      open={citation !== null}
      onClose={onClose}
      // 底部导航：上一段 / 下一段，让用户能顺着原文读上下文
      footer={
        detail ? (
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <Button
              icon={<LeftOutlined />}
              disabled={!detail.prev_chunk_id || loading}
              onClick={() => {
                if (detail.prev_chunk_id) void load(detail.doc_id, detail.prev_chunk_id);
              }}
            >
              上一段
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              第 {detail.chunk_index + 1} / {detail.total_chunks} 段
            </Typography.Text>
            <Button
              disabled={!detail.next_chunk_id || loading}
              onClick={() => {
                if (detail.next_chunk_id) void load(detail.doc_id, detail.next_chunk_id);
              }}
            >
              下一段 <RightOutlined />
            </Button>
          </Space>
        ) : null
      }
    >
      {/* 加载态：骨架占位，避免抽屉空白抖动 */}
      {loading && <Skeleton active paragraph={{ rows: 6 }} />}

      {/* 失败态：给出可读原因（如文档已下线 —— 切片已随下线删除） */}
      {!loading && error && (
        <Alert
          type="warning"
          showIcon
          message="无法打开引用原文"
          description={`${error}（若该文档已被下线或重新入库，原片段可能已不存在）`}
        />
      )}

      {/* 正常态：文档元信息 + 完整正文 */}
      {!loading && !error && detail && (
        <div>
          <Space size={8} wrap style={{ marginBottom: 12 }}>
            <Tag color="blue">{detail.file_type === 'word' ? 'Word' : 'Markdown'}</Tag>
            {detail.heading && <Tag color="geekblue">{detail.heading}</Tag>}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              切片第 {detail.chunk_index + 1} / {detail.total_chunks} 段
            </Typography.Text>
          </Space>
          {/* 正文保留换行（Markdown/Word 段落结构在切片里以换行保留） */}
          <div
            style={{
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              lineHeight: 1.75,
              fontSize: 14,
              color: '#1f1f1f',
              background: '#fafbfc',
              border: '1px solid #f0f0f0',
              borderRadius: 8,
              padding: '12px 14px',
            }}
          >
            {detail.content}
          </div>
        </div>
      )}

      {/* 兜底空态：理论上不会出现（有 citation 必有详情或错误） */}
      {!loading && !error && !detail && <Empty description="暂无内容" />}
    </Drawer>
  );
}
