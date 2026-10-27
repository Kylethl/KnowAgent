import { useCallback, useEffect, useRef, useState } from 'react';
import type { DocType, UploadItem } from '../types';
import {
  MAX_FILES_PER_BATCH,
  resolveDocType,
  uploadDocument,
  validateFile,
} from '../services/documentUpload';

// P1-F3：文档上传队列钩子
// 职责：收拢「选文件 → 校验 → 串行上传 → 进度/成功/失败 → 重试/移除」的全部状态，
//      让上传面板组件只负责渲染，不掺和业务逻辑。
//
// 设计要点：
//   1. 串行上传：一次只传一个文件，避免并发打满带宽、也让进度条含义明确。
//   2. 队列用 ref 承载：上传是命令式过程，用 ref 可避免闭包读到过期的 state。
//   3. 中断可控：每个上传都持有取消函数，移除队列项时能立刻中止传输。

// 队列中的待上传任务
interface QueueTask {
  id: string; // 对应的 UploadItem.id
  file: File; // 原始文件
  docType: DocType; // 文档类型
}

export function useDocumentUpload() {
  // 上传列表：按添加顺序排列，供面板渲染
  const [items, setItems] = useState<UploadItem[]>([]);
  // 批次级错误提示（如格式不支持、超出数量），单个文件的失败记在 item.error 上
  const [error, setError] = useState<string | null>(null);

  // 待上传队列
  const queueRef = useRef<QueueTask[]>([]);
  // 当前正在上传的任务 ID（无则为 null）
  const currentIdRef = useRef<string | null>(null);
  // 当前上传的取消函数
  const abortRef = useRef<(() => void) | null>(null);
  // items 的镜像：校验重名时需要立即读到最新列表，不能等下一轮渲染
  const itemsRef = useRef<UploadItem[]>([]);
  // pump 的自引用：串行队列需要在回调里递归调用自身
  const pumpRef = useRef<(() => void)>(() => {});

  // 生成列表项唯一 ID：时间戳 + 随机串（P2 接入后端后改用服务端返回的 id）
  const genId = () => `${Date.now()}-${Math.random().toString(36).slice(2)}`;

  /** 局部更新某个上传项（进度、状态、回执、错误） */
  const updateItem = useCallback((id: string, patch: Partial<UploadItem>) => {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...patch } : it)));
  }, []);

  /**
   * 驱动队列：若无任务在跑，则取出队首开始上传
   * 上传结束后（成功或失败）递归调用自身，继续处理下一个。
   */
  const pump = useCallback(() => {
    // 已有任务在传输中：直接返回，等它结束后再被调用
    if (currentIdRef.current !== null) return;

    const next = queueRef.current.shift();
    if (!next) return; // 队列空，自然结束

    currentIdRef.current = next.id;
    abortRef.current = uploadDocument(next.file, next.docType, {
      // 进度更新
      onProgress: (percent) => updateItem(next.id, { percent }),
      // 成功：写入后端回执，状态转为 success（对应「待审核」标签）
      onSuccess: (record) => {
        updateItem(next.id, {
          status: 'success',
          percent: 100,
          record,
          error: undefined,
        });
        currentIdRef.current = null;
        abortRef.current = null;
        pumpRef.current(); // 继续下一个
      },
      // 失败：记录原因，仍然继续下一个，避免一个坏文件堵住整批
      onError: (message) => {
        updateItem(next.id, { status: 'error', error: message });
        currentIdRef.current = null;
        abortRef.current = null;
        pumpRef.current();
      },
    });
  }, [updateItem]);

  // 让 pumpRef 始终指向最新的 pump（闭包里会递归调用它）
  useEffect(() => {
    pumpRef.current = pump;
  }, [pump]);

  /**
   * 添加待上传文件：校验 → 入列 → 驱动队列
   * @param files 用户通过拖拽或选择得到的文件列表
   */
  const addFiles = useCallback((files: File[]) => {
    // 单批次数量上限：防止一次拖入大量文件拖垮页面与后端
    if (files.length > MAX_FILES_PER_BATCH) {
      setError(`一次最多上传 ${MAX_FILES_PER_BATCH} 个文件`);
      return;
    }

    // 已有文件名集合，用于批次内/跨批次重名校验
    const nameSet = new Set(itemsRef.current.map((it) => it.file.name));
    const accepted: UploadItem[] = [];
    const rejected: string[] = [];

    for (const file of files) {
      // 逐项校验：格式 / 大小 / 空文件 / 重名
      const reason = validateFile(file, [...nameSet]);
      if (reason) {
        rejected.push(reason);
        continue;
      }
      nameSet.add(file.name);
      // 校验已确保扩展名合法，这里断言非空类型
      const docType = resolveDocType(file.name) as DocType;
      accepted.push({
        id: genId(),
        file,
        docType,
        status: 'uploading',
        percent: 0,
      });
    }

    // 校验结果反馈：全部通过则清空上一次的批次错误
    setError(rejected.length ? rejected.join('；') : null);
    if (!accepted.length) return;

    // 同步镜像并合并进列表
    itemsRef.current = [...itemsRef.current, ...accepted];
    setItems((prev) => [...prev, ...accepted]);

    // 入队并启动（若当前空闲则立即开始传输）
    queueRef.current.push(
      ...accepted.map((it) => ({ id: it.id, file: it.file, docType: it.docType }))
    );
    pumpRef.current();
  }, []);

  /**
   * 移除某个上传项
   * - 若它正在传输：中断请求并让队列继续推进
   * - 若它还在排队：直接从队列里剔除
   */
  const removeItem = useCallback((id: string) => {
    if (currentIdRef.current === id) {
      abortRef.current?.(); // 中断正在进行的传输
      abortRef.current = null;
      currentIdRef.current = null;
    } else {
      queueRef.current = queueRef.current.filter((task) => task.id !== id);
    }
    setItems((prev) => prev.filter((it) => it.id !== id));
    itemsRef.current = itemsRef.current.filter((it) => it.id !== id);
    pumpRef.current(); // 可能因为中断而空出位置
  }, []);

  /**
   * 重试上传失败的项：重置状态后重新入队
   */
  const retry = useCallback(
    (id: string) => {
      const target = itemsRef.current.find((it) => it.id === id);
      if (!target || target.status !== 'error') return;
      updateItem(id, { status: 'uploading', percent: 0, error: undefined });
      queueRef.current.push({ id, file: target.file, docType: target.docType });
      pumpRef.current();
    },
    [updateItem]
  );

  /** 清空整个上传列表（同步中断当前传输） */
  const clearAll = useCallback(() => {
    abortRef.current?.();
    abortRef.current = null;
    currentIdRef.current = null;
    queueRef.current = [];
    itemsRef.current = [];
    setItems([]);
    setError(null);
  }, []);

  /** 手动关闭批次级错误提示（用户点掉 Alert 后不再重复展示） */
  const clearError = useCallback(() => setError(null), []);

  // 组件卸载时中断传输，防止 setState 泄漏
  useEffect(() => {
    return () => {
      abortRef.current?.();
    };
  }, []);

  return { items, error, addFiles, removeItem, retry, clearAll, clearError };
}
