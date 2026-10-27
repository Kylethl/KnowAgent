-- 004 · P3-DB2：启用 pgvector 并建文档切片向量表
-- ---------------------------------------------------------------------
-- 对应《需求清单》P3 功能2「文档入库（RAG 管线）」
--   - 数据存储：PostgreSQL 启用 pgvector；向量表（chunk_id, doc_id, embedding,
--               content, metadata）；documents.status = indexed
-- 对应任务清单：P3-DB2
--
-- 约定与说明：
--   1. 本文件由 app/db/init_db.py 按「分号」切分后逐条执行，
--      因此**不要**写 PL/pgSQL（DO $$ ... $$）块——其内部语句会被错误切分。
--      全部使用幂等 DDL（IF NOT EXISTS），重复执行安全。
--   2. 向量维度固定 1024：与 config.EMBEDDING_DIM 对齐
--      （qwen3-vl-embedding 支持 2560/2048/1536/1024/768/512/256，
--        综合存储与召回效果取 1024）。
--      ⚠️ 若更换维度，必须 DROP 本表后重建（vector 列维度不可原地修改）。
--   3. 扩展创建需要数据库超级用户权限；Docker 镜像的 knowagent 用户具备该权限。
--      存量库迁移请手动执行一次本文件首条语句。

-- ---------- 1. 启用 pgvector 扩展 ----------
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------- 2. 文档切片向量表 ----------
CREATE TABLE IF NOT EXISTS document_chunks (
    -- 切片主键
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 所属文档；文档被删除时切片级联删除（ON DELETE CASCADE）
    doc_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    -- 切片在文档内的序号（从 0 开始），保证「按原文顺序」可还原
    chunk_index INTEGER NOT NULL,
    -- 切片正文（入库检索的最小粒度）
    content TEXT NOT NULL,
    -- 切片估算 token 数（便于后续上下文预算管理与统计）
    token_count INTEGER,
    -- 向量：qwen3-vl-embedding 生成的 1024 维文本片段向量
    embedding vector(1024),
    -- 切片元信息：{filename, file_type, heading, ...}，供 P4 引用卡片展示
    metadata JSONB,
    -- 创建时间
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 同一文档内切片序号唯一：重入库时可用 ON CONFLICT 覆盖，也防止重复插入
    CONSTRAINT uq_document_chunks_doc_index UNIQUE (doc_id, chunk_index)
);

-- ---------- 3. 索引 ----------
-- 按文档查切片 / 重入库前删除旧切片（高频）
CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_id ON document_chunks (doc_id);

-- 向量相似度索引（HNSW + 余弦距离），供 P4 检索召回使用。
-- 说明：HNSW 在 pgvector 0.5+ 可用；相比 IVFFlat 无需预训练、召回更稳，
--       代价是构建稍慢、内存占用略高——对本项目规模（万级切片）完全可接受。
CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding
    ON document_chunks USING hnsw (embedding vector_cosine_ops);

-- ---------- 4. documents 表扩展入库字段 ----------
-- 入库完成时间；未入库为 NULL（区别于审核时间 reviewed_at）
ALTER TABLE documents ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ;
-- 最近一次入库失败的原因；成功入库后置空，便于管理员排查（前端展示「入库失败」）
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_index_error TEXT;
