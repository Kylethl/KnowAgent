-- =====================================================================
-- 知识库问答网站 — P6-DB2 建表脚本（PostgreSQL）：feedback 表
-- ---------------------------------------------------------------------
-- 依据：
--   《知识库网站需求清单与技术栈（v2）》P6 功能2「答案反馈与评估」
--     前端：回答点赞/点踩按钮 + 反馈表单
--     后端：/messages/{id}/feedback 存储反馈
--     AI Agent：反馈采集用于后续 prompt/检索离线优化
--     数据存储：feedback 表
--   《开发任务清单》P6-B2 / P6-DB2
--
-- 设计要点：
--   1. 一条反馈指向「一条助手回答」（messages.id），而不是会话或用户：
--      评估的对象是「这一次回答好不好」，粒度错了就没法做离线优化。
--   2. (message_id, user_id) 唯一：同一用户对同一条回答只保留一条反馈，
--      再次提交视为**修改**（先赞后踩很常见，不应堆积两条互相矛盾的记录）。
--   3. rating 只有 up / down 两种取值，与前端按钮严格对应；
--      reason 是点踩时的归因分类（答非所问 / 内容错误 / 资料不符 / 其他），
--      便于统计「主要问题类型」，这是后续优化 prompt 与检索的直接输入。
--   4. 行随消息级联删除（ON DELETE CASCADE）：会话被彻底清理时反馈一并消失，
--      不会留下悬空引用污染统计。
--   5. user_id 采用 ON DELETE SET NULL：用户注销后反馈作为「匿名评价」保留，
--      统计口径（这个回答被踩过几次）不应因用户离开而失真。
--
-- 依赖：
--   messages 表（001）、users 表（001）已存在。
--
-- 用法：
--   psql "$DATABASE_URL" -f sql/008_p6_feedback.sql
--   或：cd backend && python -m app.db.init_db
--
-- 幂等：
--   全部使用 CREATE TABLE / CREATE INDEX IF NOT EXISTS，可重复执行。
-- 注意：
--   init_db.split_statements 按顶层分号切分语句，本文件**注释中不得出现半角分号**。
-- =====================================================================


-- ---------------------------------------------------------------------
-- feedback：答案反馈（点赞 / 点踩）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feedback (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),   -- 反馈主键
    -- 被评价的消息：必须是助手回答（service 层校验），消息删除时反馈级联清理
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    -- 评价人：用户注销后置空，反馈保留为匿名评价
    user_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    -- 评价结论：up（点赞）/ down（点踩）
    rating     VARCHAR(8) NOT NULL CHECK (rating IN ('up', 'down')),
    -- 点踩归因：off_topic(答非所问) / wrong(内容错误) / unsupported(资料不符) / other(其他)
    -- 点赞时一般为空
    reason     VARCHAR(32)
               CHECK (reason IS NULL OR reason IN ('off_topic', 'wrong', 'unsupported', 'other')),
    -- 用户补充说明（自由文本，可为空）；限长在应用层校验，避免超长文本进库
    comment    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 唯一约束：同一用户对同一条回答只有一条反馈（重复提交=修改）
CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_message_user
    ON feedback (message_id, user_id);

-- 索引：管理员按「评价结论 + 时间倒序」看差评列表（评估与优化入口）
CREATE INDEX IF NOT EXISTS idx_feedback_rating_created
    ON feedback (rating, created_at DESC);


-- ---------------------------------------------------------------------
-- 字段说明（供运维查阅，重复执行无副作用）
-- ---------------------------------------------------------------------
COMMENT ON TABLE feedback IS 'P6：答案反馈（点赞/点踩）';
COMMENT ON COLUMN feedback.rating IS 'P6：评价结论 up / down';
COMMENT ON COLUMN feedback.reason IS 'P6：点踩归因 off_topic / wrong / unsupported / other';
