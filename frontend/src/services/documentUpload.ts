import type { DocType, DocumentRecord } from '../types';
import { getToken, triggerUnauthorized } from './http';

// P1-F3 / P5-F1：文档上传服务
// 职责：把用户选择的 Word / Markdown / 图片 / 音频 / 视频文件以 multipart/form-data
//      上传到后端，并实时回传上传进度、成功回执与失败原因。
//
// 为什么不用 fetch / EventSource：
//   - fetch 至今无法监听「请求体发送进度」，拿不到上传百分比；
//   - 需求明确要求「显示上传进度」，因此这里用 XMLHttpRequest（xhr.upload.onprogress）。
//
// 后端契约（P1-B2 / P5-B1）：
//   POST /documents/upload   Content-Type: multipart/form-data
//   字段：file（必填，原始文件）、type（word/markdown/image/audio/video，便于后端二次校验）
//   成功 201：{ id, filename, type, status, size, storage_path, created_at }
//   失败：FastAPI 默认形如 { "detail": "错误信息" }

// P5：扩展名白名单**按类型分组**。
// 后端 services/media_types.py 中的 EXTENSION_TO_TYPE 是唯一真源，两边必须保持一致；
// 分组的意义在于「一个类型一个大小上限」，而不是所有文件共用一个 20MB。
export const EXTENSIONS_BY_TYPE: Record<DocType, readonly string[]> = {
  word: ['.doc', '.docx'],
  markdown: ['.md'],
  image: ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'],
  audio: ['.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg'],
  video: ['.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'],
};

// 所有允许的扩展名（用于 <input accept> 与展示文案）
export const ALLOWED_EXTENSIONS: readonly string[] = Object.values(EXTENSIONS_BY_TYPE).flat();

// 各类型单文件大小上限（MB）：与后端 config.MAX_UPLOAD_SIZE / MAX_IMAGE_SIZE /
// MAX_AUDIO_SIZE / MAX_VIDEO_SIZE 保持一致，避免「前端放行、后端 413」的糟糕体验。
export const MAX_SIZE_MB_BY_TYPE: Record<DocType, number> = {
  word: 20,
  markdown: 20,
  image: 10,
  audio: 50,
  video: 200,
};

// 类型展示名（用于错误提示，避免用户看到英文类型名）
const TYPE_LABEL: Record<DocType, string> = {
  word: 'Word',
  markdown: 'Markdown',
  image: '图片',
  audio: '音频',
  video: '视频',
};

// 兼容旧引用的默认上限（文档类）；媒体类请用 maxSizeMbFor()
export const MAX_FILE_SIZE_MB = MAX_SIZE_MB_BY_TYPE.word;

// 单批次最多选择的文件数量：防止一次拖入上百个文件把浏览器与后端打满
export const MAX_FILES_PER_BATCH = 10;

// 上传接口地址：默认 /api/documents/upload（nginx / Vite 代理剥前缀后指向后端 /documents/upload）
// 如需直连后端，可通过 .env 配置 VITE_UPLOAD_API 覆盖
const UPLOAD_ENDPOINT: string =
  import.meta.env.VITE_UPLOAD_API ?? '/api/documents/upload';

// multipart/form-data 中的文件字段名，必须与后端 P1-B2 保持一致
const FILE_FIELD_NAME = 'file';

// 传输阶段进度上限：真实进度到 100% 只代表「字节发完」，
// 后端还要写对象存储与落库，这段等待时间用 95% 表达，收到响应后再补到 100%。
const TRANSFER_PERCENT_CAP = 95;

// 是否启用本地模拟上传：后端 P1-B2 未就绪时，用于独立验收前端 UI（进度 / 待审核标签）
// 开启方式：frontend/.env 中设置 VITE_UPLOAD_MOCK=true
const USE_MOCK_UPLOAD: boolean = import.meta.env.VITE_UPLOAD_MOCK === 'true';

// 上传过程的回调集合
export interface UploadHandlers {
  onProgress: (percent: number) => void; // 进度更新（0-100）
  onSuccess: (record: DocumentRecord) => void; // 上传成功，返回文档记录
  onError: (message: string) => void; // 上传失败，返回可读原因
}

/**
 * 从文件名推导文档类型
 * @param filename 文件名（含扩展名）
 * @returns 文档类型；扩展名不在白名单内时返回 null
 */
export function resolveDocType(filename: string): DocType | null {
  const lower = filename.toLowerCase();
  // 按类型分组匹配：顺序无关，且新增类型只需改 EXTENSIONS_BY_TYPE
  for (const [type, exts] of Object.entries(EXTENSIONS_BY_TYPE) as [DocType, readonly string[]][]) {
    if (exts.some((ext) => lower.endsWith(ext))) return type;
  }
  return null;
}

/**
 * 按类型取单文件大小上限（MB）
 * @param type 文档类型
 */
export function maxSizeMbFor(type: DocType): number {
  return MAX_SIZE_MB_BY_TYPE[type] ?? MAX_FILE_SIZE_MB;
}

/**
 * 判断文件名是否在允许的格式白名单内
 * @param filename 文件名（含扩展名）
 */
export function isAllowedExtension(filename: string): boolean {
  const lower = filename.toLowerCase();
  return ALLOWED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

/**
 * 上传前校验单个文件
 * 校验项：格式白名单 → 类型推导 → 该类型的大小上限 → 空文件 → 同名重复
 * @param file 待校验的文件
 * @param existingNames 队列中已有的文件名（用于去重）
 * @returns 错误文案；校验通过返回 null
 */
export function validateFile(
  file: File,
  existingNames: readonly string[]
): string | null {
  // 1) 格式校验：按扩展名白名单判定
  const type = resolveDocType(file.name);
  if (!type) {
    return `「${file.name}」格式不支持，仅可上传 ${ALLOWED_EXTENSIONS.join(' / ')}`;
  }
  // 2) 大小校验：按类型取上限（图片 10MB / 音频 50MB / 视频 200MB / 文档 20MB）
  const limitMb = maxSizeMbFor(type);
  if (file.size > limitMb * 1024 * 1024) {
    return `「${file.name}」超过${TYPE_LABEL[type]} ${limitMb}MB 上限（实际 ${formatFileSize(file.size)}）`;
  }
  // 3) 空文件：0 字节文件没有入库价值，且会让后端解析阶段报错
  if (file.size === 0) {
    return `「${file.name}」是空文件，无法上传`;
  }
  // 4) 同名去重：同一批次内不允许重名，避免后端落盘覆盖
  if (existingNames.includes(file.name)) {
    return `「${file.name}」已在上传列表中`;
  }
  return null;
}

/**
 * 把字节数格式化为可读文本（B / KB / MB / GB）
 * @param bytes 文件字节数
 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/**
 * 上传单个文档
 * @param file 待上传的文件（已通过 validateFile 校验）
 * @param docType 由扩展名推导出的文档类型
 * @param handlers 进度 / 成功 / 失败回调
 * @returns 取消函数；调用即中断本次上传（不再触发后续回调）
 */
export function uploadDocument(
  file: File,
  docType: DocType,
  handlers: UploadHandlers
): () => void {
  // 本地模拟模式：不走网络，直接伪造进度与回执，仅供 UI 验收
  if (USE_MOCK_UPLOAD) {
    return mockUpload(file, docType, handlers);
  }

  // 已取消标记：区分「用户主动取消」与「网络异常中断」
  let cancelled = false;
  const xhr = new XMLHttpRequest();

  // 组装表单：文件名保留原始名称，后端负责重命名落盘
  const form = new FormData();
  form.append(FILE_FIELD_NAME, file, file.name);
  form.append('type', docType); // 附带类型，便于后端二次校验（不信任前端）

  xhr.open('POST', UPLOAD_ENDPOINT);

  // P2-F2：附带 JWT。上传走 XMLHttpRequest（为拿上传进度），可以正常设置请求头，
  // 优于 SSE 只能把令牌放 query 的做法。提交人由后端从令牌解析，前端不再传 user_id。
  const token = getToken();
  if (token) {
    xhr.setRequestHeader('Authorization', `Bearer ${token}`);
  }

  // 传输进度：只在能算出总量时上报，上限压到 95% 留出服务端处理时间
  xhr.upload.onprogress = (event: ProgressEvent) => {
    if (!event.lengthComputable) return;
    const ratio = event.loaded / event.total;
    handlers.onProgress(Math.min(TRANSFER_PERCENT_CAP, Math.round(ratio * TRANSFER_PERCENT_CAP)));
  };

  // 收到响应：2xx 解析回执，非 2xx 转成可读错误
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      try {
        const record = JSON.parse(xhr.responseText) as DocumentRecord;
        handlers.onProgress(100); // 字节发完 + 服务端处理完成，进度补齐
        handlers.onSuccess(record);
      } catch {
        handlers.onError('服务端返回内容无法解析，请联系管理员');
      }
      return;
    }
    handlers.onError(parseErrorMessage(xhr));
  };

  // 网络层异常：连接失败 / 超时；主动取消不报错
  xhr.onerror = () => {
    if (!cancelled) handlers.onError('网络异常，上传失败');
  };
  xhr.ontimeout = () => {
    if (!cancelled) handlers.onError('上传超时，请稍后重试');
  };
  xhr.onabort = () => {
    if (!cancelled) handlers.onError('上传已中断');
  };

  xhr.send(form);

  // 返回取消函数：标记后 abort，避免回调误报错误
  return () => {
    cancelled = true;
    xhr.abort();
  };
}

/**
 * 把非 2xx 响应转换为用户可读的错误文案
 * 优先取 FastAPI 的 detail 字段；不同状态码给出不同兜底提示。
 */
function parseErrorMessage(xhr: XMLHttpRequest): string {
  try {
    const body = JSON.parse(xhr.responseText) as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
  } catch {
    // 响应体不是 JSON（如 nginx 的 HTML 错误页），落到状态码兜底
  }
  if (xhr.status === 401) {
    // 令牌失效：清理本地状态并跳登录页（上传走 XHR，不走 requestJson，需手动触发）
    triggerUnauthorized();
    return '登录已失效，请重新登录';
  }
  if (xhr.status === 404) return '上传接口不存在，请确认后端服务版本';
  if (xhr.status === 413) return '文件过大，超过服务端对该类型的大小限制';
  if (xhr.status === 415) return '服务端拒绝了该文件格式（或文件内容与扩展名不符）';
  if (xhr.status === 0) return '无法连接服务器，请检查后端是否已启动';
  return `上传失败（HTTP ${xhr.status}）`;
}

/**
 * 本地模拟上传：不发起网络请求，按时间推进度并返回伪造回执
 * 用途：后端 P1-B2 未就绪时，独立验收前端的进度条与「待审核」标签。
 */
function mockUpload(
  file: File,
  docType: DocType,
  handlers: UploadHandlers
): () => void {
  let percent = 0;
  let finished = false;
  // 定时器：每 150ms 推进一截进度，模拟真实网络上传节奏
  const timer = window.setInterval(() => {
    percent = Math.min(TRANSFER_PERCENT_CAP, percent + 8 + Math.random() * 12);
    handlers.onProgress(Math.round(percent));
    if (percent >= TRANSFER_PERCENT_CAP) {
      window.clearInterval(timer);
      // 模拟服务端写对象存储 + 落库的耗时，再返回成功回执
      window.setTimeout(() => {
        if (finished) return;
        finished = true;
        handlers.onProgress(100);
        handlers.onSuccess({
          id: `mock-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          filename: file.name,
          file_type: docType,
          status: 'pending', // P1 上传成功即为「待审核」
          size_bytes: file.size,
          original_path: `mock/raw/${file.name}`,
          created_at: new Date().toISOString(),
        });
      }, 400);
    }
  }, 150);

  // 取消函数：停掉定时器，后续不再回调
  return () => {
    finished = true;
    window.clearInterval(timer);
  };
}
