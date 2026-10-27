-- =====================================================================
-- 知识库问答网站 — P1-DB1 建表脚本（PostgreSQL）
-- ---------------------------------------------------------------------
-- 依据：
--   《知识库网站需求清单与技术栈（v2）》+《开发任务清单》P1-DB1
--   +《数据库表结构 DDL 初稿》
-- 范围：
--   本阶段（P1 地基）仅建 conversations / messages 两张核心表。
--   因 conversations.user_id 外键引用 users(id)，users 表作为前置
--   依赖一并建立（users 的业务逻辑属于 P2-B2，本阶段仅建表占位）。
-- 数据库：
--   PostgreSQL 13+（gen_random_uuid() 内置，无需 uuid-ossp 扩展）。
--   本阶段【不】启用 pgvector；向量能力在 P3-DB2 引入。
-- 用法：
--   psql "$DATABASE_URL" -f sql/001_p1_db1_schema.sql
--   或：python -m app.db.init_db
-- 幂等：
--   全部使用 CREATE TABLE / CREATE INDEX IF NOT EXISTS，可重复执行。
-- =====================================================================


-- ---------------------------------------------------------------------
-- 0. 前置：用户表（P2-B2 业务表，本阶段作为 conversations 外键前置建立）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- 主键，默认用 PG 内置随机 UUID
    username      VARCHAR(64)  NOT NULL UNIQUE,                -- 登录名，唯一
    email         VARCHAR(255) UNIQUE,                         -- 邮箱，唯一（可空）
    password_hash VARCHAR(255) NOT NULL,                       -- bcrypt 等加密后的密码，绝不存明文
    role          VARCHAR(16)  NOT NULL DEFAULT 'user'         -- 角色：user / admin（P2 RBAC 用）
                  CHECK (role IN ('user', 'admin')),
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,          -- 账号是否启用（P2 软封禁用）
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),         -- 创建时间（服务端时区）
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()          -- 更新时间
);


-- ---------------------------------------------------------------------
-- 1. 会话表（P1 核心表）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),    -- 会话主键
    -- 所属用户：用户被删时会话级联删除（ON DELETE CASCADE）
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       VARCHAR(255),                                  -- 会话标题（可首条问题摘要自动生成）
    -- 模型字段：按《需求清单》锁定 DeepSeek-Flash。
    --   说明：DDL 初稿曾写 'deepseek-v4.1-flash'，但该名在 DeepSeek 在线 API 不可用，
    --        实际可用模型为 deepseek-flash / deepseek-v4-pro；此处与后端
    --        config.DEEPSEEK_MODEL 对齐，遵循《需求清单》"LLM = DeepSeek-Flash" 的约定。
    model       VARCHAR(64) NOT NULL DEFAULT 'deepseek-flash',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ                                     -- 软删除时间戳，空=未删除（P2 删除用）
);

-- 索引：按用户筛「未删除」会话并按更新时间倒序，支撑左侧会话列表（P2）
CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
    ON conversations (user_id, updated_at DESC)
    WHERE deleted_at IS NULL;


-- ---------------------------------------------------------------------
-- 2. 消息表（P1 核心表，先存文本；P4 追加引用来源）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(), -- 消息主键
    -- 所属会话：会话被删时消息级联删除（ON DELETE CASCADE）
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            VARCHAR(16) NOT NULL                        -- 角色：user / assistant / system
                    CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT        NOT NULL,                       -- 消息正文
    token_count     INTEGER,                                    -- 令牌数，上下文窗口管理（P2）用
    citations       JSONB,                                       -- P4：引用来源 [{doc_id, chunk, snippet}]
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()          -- 发送时间
);

-- 索引：按会话 + 时间排序，支撑历史回看（P2）
CREATE INDEX IF NOT EXISTS idx_messages_conv_created
    ON messages (conversation_id, created_at);
