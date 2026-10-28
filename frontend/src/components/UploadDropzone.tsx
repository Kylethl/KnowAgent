import {
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type KeyboardEvent,
} from 'react';
import { InboxOutlined } from '@ant-design/icons';
import { Typography } from 'antd';
import { ALLOWED_EXTENSIONS, MAX_SIZE_MB_BY_TYPE } from '../services/documentUpload';

// P1-F3 / P5-F1：文档拖拽上传区
// 职责：提供「点击选择 + 拖拽投放」两种入口，把拿到的 File 列表交给上层。
//
// 为什么不直接用 antd 的 Upload.Dragger：
//   需求要求「显示上传进度 + 待审核标签」，上传队列由 useDocumentUpload 自己管理；
//   antd Upload 内部也维护一份 fileList，两套状态并存容易不一致。
//   这里只借用它的视觉规范（虚线框 + 图标 + 说明文案），逻辑保持自有可控。

interface Props {
  onFiles: (files: File[]) => void; // 选中/拖入文件后的回调
  disabled?: boolean; // 禁用态（如正在关闭抽屉）
}

export default function UploadDropzone({ onFiles, disabled = false }: Props) {
  // 隐藏的原生文件选择框，由点击行为间接触发
  const inputRef = useRef<HTMLInputElement>(null);
  // 是否处于拖拽悬停态：用于高亮提示「松手即可上传」
  const [isDragOver, setIsDragOver] = useState(false);

  /** 点击区域：等价于点击隐藏的文件选择框 */
  const handleClick = () => {
    if (!disabled) inputRef.current?.click();
  };

  /** 键盘可达：Enter / Space 触发选择，保证无障碍 */
  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault(); // 阻止空格滚动页面
      handleClick();
    }
  };

  /** 拖拽悬停：必须 preventDefault，否则浏览器会自行打开被拖入的文件 */
  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (!disabled) setIsDragOver(true);
  };

  /** 拖拽离开：取消高亮 */
  const handleDragLeave = () => setIsDragOver(false);

  /** 放下文件：取出全部文件交给上层校验与上传 */
  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault(); // 同样阻止浏览器默认打开行为
    setIsDragOver(false);
    if (disabled) return;
    const files = Array.from(e.dataTransfer.files ?? []);
    if (files.length) onFiles(files);
  };

  /**
   * 文件选择框变化
   * 处理完立刻清空 value，否则再次选择同一个文件不会触发 change 事件。
   */
  const handleInputChange = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (files.length) onFiles(files);
    e.target.value = '';
  };

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label="拖拽或点击上传文档"
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      style={{
        border: `1px dashed ${isDragOver ? '#386BF3' : '#d9d9d9'}`,
        background: isDragOver ? '#f0f5ff' : '#fafafa',
        borderRadius: 12,
        padding: '28px 16px',
        textAlign: 'center',
        cursor: disabled ? 'not-allowed' : 'pointer',
        transition: 'border-color .2s, background .2s',
        outline: 'none',
      }}
    >
      {/* 上传图标：拖拽悬停时变为主色，强化可投放反馈 */}
      <InboxOutlined
        style={{ fontSize: 34, color: isDragOver ? '#386BF3' : '#bfbfbf' }}
      />
      <div style={{ marginTop: 10 }}>
        <Typography.Text strong>点击选择文件，或拖拽到此处</Typography.Text>
      </div>
      <div style={{ marginTop: 6 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          支持 Word / Markdown、图片（{MAX_SIZE_MB_BY_TYPE.image}MB）、
          音频（{MAX_SIZE_MB_BY_TYPE.audio}MB）、视频（{MAX_SIZE_MB_BY_TYPE.video}MB）
        </Typography.Text>
      </div>

      {/* 隐藏的文件选择框：accept 仅做系统层面的过滤提示，真正校验在 validateFile */}
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ALLOWED_EXTENSIONS.join(',')}
        onChange={handleInputChange}
        style={{ display: 'none' }}
      />
    </div>
  );
}
