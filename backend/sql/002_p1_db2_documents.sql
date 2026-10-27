-- =====================================================================
-- 知识库问答网站 — P1-DB2 建表脚本（PostgreSQL）：documents 表
-- ---------------------------------------------------------------------
-- 依据：
--   《知识库网站需求清单与技术栈（v2）》+《开发任务清单》P1-B2 / P1-DB2
--   +《数据库表结构 DDL 初稿》（documents 表定义）
-- 范围：
--   建 documents 表（用户提交文档的元数据）。原始文件不进库，仅存对象存储相对路径。
--   本表是 P1-B2「/documents/upload 落库」的前置；审核 / 入库相关字段（reviewer_*、
--   chunk_count 等）属 P3，此处一并建列，避免后续频繁改表。
-- 依赖：
--   users 表（由 001_p1_db1_schema.sql 建立），documents.user_id / reviewer_id 引用之。
-- 数据库：
--   PostgreSQL 13+（gen_random_uuid() 内置，无需 uuid-ossp 扩展）。
-- 用法：
--   psql "$DATABASE_URL" -f sql/002_p1_db2_documents.sql
--   或：python -m app.db.init_db            -- init_db 会按序执行 sql/00N_*.sql 全部文件
-- 幂等：
--   全部使用 CREATE TABLE / CREATE INDEX IF NOT EXISTS，可重复执行。
-- =====================================================================


-- ---------------------------------------------------------------------
-- documents：文档提交表（P1 核心表之一）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- 文档主键
    -- 提交人：P1 阶段无登录鉴权，允许为空（NULL）；用户被删时置 NULL 而非级联删文档
    user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    filename      VARCHAR(512) NOT NULL,                     -- 原始文件名（展示用）
    original_path VARCHAR(1024) NOT NULL,                    -- 对象存储相对路径（不含 bucket），二进制不进库
    -- 文档类型：word(.doc/.docx) / markdown(.md)；P5 扩展 image/audio/video
    file_type     VARCHAR(16) NOT NULL
                  CHECK (file_type IN ('word', 'markdown', 'image', 'audio', 'video')),
    mime_type     VARCHAR(128),                              -- MIME 类型（预览/下载用）
    size_bytes    BIGINT,                                    -- 文件字节数
    -- 生命周期状态：pending(待审核) → approved/rejected(审核) → indexing/indexed(入库)
    status        VARCHAR(24) NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected', 'indexing', 'indexed')),
    -- ---- P3 审核字段（本阶段仅建列） ----
    reviewer_id   UUID REFERENCES users(id) ON DELETE SET NULL,  -- 审核人（管理员）
    review_note   TEXT,                                      -- 审核备注（驳回理由）
    reviewed_at   TIMESTAMPTZ,                               -- 审核时间
    chunk_count   INTEGER NOT NULL DEFAULT 0,                -- 入库后切片数（P3 回填）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),         -- 提交时间
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),         -- 更新时间
    deleted_at    TIMESTAMPTZ                                 -- 软删除时间戳，空=未删除
);

-- 索引：按用户 + 状态筛文档（本人文档列表 / 审核队列用）
CREATE INDEX IF NOT EXISTS idx_documents_user_status
    ON documents (user_id, status);

-- 索引：按创建时间倒序（列表分页用）
CREATE INDEX IF NOT EXISTS idx_documents_created
    ON documents (created_at DESC);
