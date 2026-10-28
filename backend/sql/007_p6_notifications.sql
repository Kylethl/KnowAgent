-- =====================================================================
-- 知识库问答网站 — P6-DB1 建表脚本（PostgreSQL）：notifications 表
-- ---------------------------------------------------------------------
-- 依据：
--   《知识库网站需求清单与技术栈（v2）》P6 功能1「审核通知」
--     前端：站内信 / Toast 提示「文档已通过 / 被驳回」
--     后端：状态变更触发通知（通过/驳回后发消息）
--     数据存储：notifications 表
--   《开发任务清单》P6-B1 / P6-DB1
--
-- 设计要点：
--   1. 通知是「写多读少、按人按时间查」的表，因此以 user_id + created_at 建索引，
--      未读角标另建部分索引（WHERE read_at IS NULL），避免全表统计未读数。
--   2. 通知正文在**生成时**就固化（title / content 存快照），不依赖 documents 表实时拼装：
--      文档改名、被删除、审核备注被改都不应让历史通知变成空白或另一种说法。
--   3. doc_id 采用 ON DELETE SET NULL 而非 CASCADE：文档被清理后，
--      「你提交的文档已通过」这条通知本身仍是有效的事实记录，不该跟着消失。
--   4. type 用字符串枚举而非 PG ENUM 类型：新增通知类型只需放宽 CHECK 约束，
--      不必 ALTER TYPE（PG ENUM 加值不可回滚，运维成本更高）。
--
-- 依赖：
--   users 表（001）、documents 表（002）已存在。
--
-- 用法：
--   psql "$DATABASE_URL" -f sql/007_p6_notifications.sql
--   或：cd backend && python -m app.db.init_db
--
-- 幂等：
--   全部使用 CREATE TABLE / CREATE INDEX IF NOT EXISTS，可重复执行。
-- 注意：
--   init_db.split_statements 按顶层分号切分语句，本文件**注释中不得出现半角分号**。
-- =====================================================================


-- ---------------------------------------------------------------------
-- notifications：站内信（审核结果通知）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),   -- 通知主键
    -- 收件人：通知必须有人可收，故 NOT NULL；用户注销时其通知一并清理
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- 通知类型：document_approved（审核通过）/ document_rejected（审核驳回）
    type       VARCHAR(32) NOT NULL
               CHECK (type IN ('document_approved', 'document_rejected')),
    -- 标题：一句话结论（前端列表主文案 / Toast 标题）
    title      VARCHAR(255) NOT NULL,
    -- 正文：含文件名与驳回理由等细节（前端展开查看）
    content    TEXT NOT NULL,
    -- 关联文档：便于前端「点击通知定位到我的文档」，文档被清理时置空
    doc_id     UUID REFERENCES documents(id) ON DELETE SET NULL,
    -- 已读时间：NULL 表示未读，非空即已读（用时间戳而非布尔值，便于统计已读延迟）
    read_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 索引：通知列表按「收件人 + 时间倒序」分页（最常用查询）
CREATE INDEX IF NOT EXISTS idx_notifications_user_created
    ON notifications (user_id, created_at DESC);

-- 索引：未读角标统计（部分索引只覆盖未读行，体积极小、更新代价低）
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
    ON notifications (user_id)
    WHERE read_at IS NULL;


-- ---------------------------------------------------------------------
-- 字段说明（供运维查阅，重复执行无副作用）
-- ---------------------------------------------------------------------
COMMENT ON TABLE notifications IS 'P6：站内信（审核结果通知）';
COMMENT ON COLUMN notifications.type IS 'P6：通知类型 document_approved / document_rejected';
COMMENT ON COLUMN notifications.doc_id IS 'P6：关联文档（文档删除后置空，通知保留）';
COMMENT ON COLUMN notifications.read_at IS 'P6：已读时间（空=未读）';
