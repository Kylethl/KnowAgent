-- P5 功能2：多模态解析结果落库
-- ---------------------------------------------------------------------
-- 背景：《需求清单》P5 功能2 要求图片 OCR / 音视频 ASR 后向量入库。
--       转写/识别出来的文本本身就是「知识」的载体，需要留痕：
--         1. 管理员审核时能看到「这张图/这段音频到底解析出了什么」；
--         2. 排查「显示已入库但检索不到」时，可对比 extracted_text 与切片内容；
--         3. 重入库失败时旧结果不会被清空，便于对比。
--
-- 字段说明：
--   extracted_text  OCR/ASR 提取出的纯文本（文档类为 NULL）
--   extract_meta    解析元信息 JSONB：kind/provider/model/chars/elapsed_ms/
--                   duration_seconds/segments/extractor/ocr_frames
--
-- 幂等：全部使用 IF NOT EXISTS，可重复执行（生产升级安全）。
-- 执行：cd backend && python -m app.db.init_db --schema sql/006_p5_multimodal.sql

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS extracted_text TEXT;

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS extract_meta JSONB;

COMMENT ON COLUMN documents.extracted_text IS 'P5：OCR/ASR 提取出的纯文本（文档类为空）';
COMMENT ON COLUMN documents.extract_meta IS 'P5：多模态解析元信息（模型/耗时/时长/段数等）';
