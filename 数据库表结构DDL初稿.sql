-- =====================================================================
-- 知识库问答网站 — 数据库表结构 DDL 初稿 (PostgreSQL + pgvector)
-- 基于《分阶段需求清单与技术栈（v2）》
-- 适用版本：PostgreSQL 14+ ，需 pgvector 扩展
-- 注意：向量维度 DIM 需按 qwen3-vl-embedding 实际输出维度修改（常见 1024 / 2048）
--       建表前请先确认模型维度，否则向量列维度错误会导致写入失败。
-- =====================================================================

-- 0. 扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";      -- 生成 uuid（PG13+ 也可直接用 gen_random_uuid()）
CREATE EXTENSION IF NOT EXISTS "vector";          -- pgvector 向量类型

-- 1. 用户表 (P2)
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      VARCHAR(64) NOT NULL UNIQUE,
    email         VARCHAR(255) UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role          VARCHAR(16) NOT NULL DEFAULT 'user'
                  CHECK (role IN ('user', 'admin')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. 会话表 (P1)
CREATE TABLE conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       VARCHAR(255),                     -- 可自动生成首条问题摘要
    model       VARCHAR(64) NOT NULL DEFAULT 'deepseek-v4.1-flash',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ                        -- 软删除
);
CREATE INDEX idx_conversations_user_updated ON conversations (user_id, updated_at DESC)
    WHERE deleted_at IS NULL;

-- 3. 消息表 (P1)
CREATE TABLE messages (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role          VARCHAR(16) NOT NULL
                  CHECK (role IN ('user', 'assistant', 'system')),
    content       TEXT NOT NULL,
    token_count   INTEGER,                         -- 上下文窗口管理用
    citations     JSONB,                           -- P4：引用来源 [{doc_id, chunk, snippet}]
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_conv_created ON messages (conversation_id, created_at);

-- 4. 文档提交表 (P1 建，P3 扩展审核字段)
CREATE TABLE documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 提交人
    filename      VARCHAR(512) NOT NULL,
    original_path VARCHAR(1024) NOT NULL,         -- 对象存储路径（不存二进制）
    file_type     VARCHAR(16) NOT NULL
                  CHECK (file_type IN ('word', 'markdown', 'image', 'audio', 'video')),
    mime_type     VARCHAR(128),
    size_bytes    BIGINT,
    status        VARCHAR(24) NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected', 'indexing', 'indexed')),
    -- P3 审核字段
    reviewer_id   UUID REFERENCES users(id),       -- 管理员
    review_note   TEXT,
    reviewed_at   TIMESTAMPTZ,
    chunk_count   INTEGER DEFAULT 0,               -- 入库后切片数
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ                       -- 软删除
);
CREATE INDEX idx_documents_user_status ON documents (user_id, status);

-- 5. 文档切片向量表 (P3, pgvector)
--    DIM 需替换为 qwen3-vl-embedding 的实际维度
CREATE TABLE document_chunks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL,
    content       TEXT NOT NULL,
    embedding     VECTOR(1024),                    -- TODO: 改为实际维度
    metadata      JSONB,                            -- 页码/来源/类型等
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chunks_doc ON document_chunks (document_id);
-- HNSW 向量索引（余弦距离），提升相似检索性能
CREATE INDEX idx_chunks_embedding ON document_chunks
    USING hnsw (embedding vector_cosine_ops);

-- 6. 通知表 (P6)
CREATE TABLE notifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        VARCHAR(32) NOT NULL
                CHECK (type IN ('doc_approved', 'doc_rejected')),
    title       VARCHAR(255),
    content     TEXT,
    is_read     BOOLEAN NOT NULL DEFAULT FALSE,
    document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_notifications_user_read ON notifications (user_id, is_read);

-- 7. 反馈表 (P6)
CREATE TABLE feedback (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id  UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rating      VARCHAR(8) NOT NULL
                CHECK (rating IN ('up', 'down')),
    comment     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_feedback_message ON feedback (message_id);

-- =====================================================================
-- 附：常用检索示例（P4 后端参考）
--   SELECT content, 1 - (embedding <=> :query_vec) AS score
--   FROM document_chunks
--   ORDER BY embedding <=> :query_vec
--   LIMIT 10;
--   （<=> 为余弦距离；1 - 距离 = 相似度）
-- =====================================================================
