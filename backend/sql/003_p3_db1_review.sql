-- =====================================================================
-- 知识库问答网站 — P3-DB1 审核字段扩展脚本（PostgreSQL）：documents 表
-- ---------------------------------------------------------------------
-- 依据：
--   《知识库网站需求清单与技术栈（v2）》P3 功能1「管理员审核」
--     数据存储：documents 扩展 review_status、reviewer、review_note、reviewed_at
--   《开发任务清单》P3-DB1
-- 背景：
--   002 脚本已建好 reviewer_id / review_note / reviewed_at / chunk_count 四列，
--   但**缺需求点名的 review_status**，本脚本补上该列并完成存量数据回填。
--
-- 字段职责划分（重要，避免与 status 混淆）：
--   status        整体生命周期：pending → approved/rejected → indexing → indexed
--   review_status 审核结论（本脚本新增）：pending / approved / rejected
--   两者在审核动作中同步写入，但语义独立：
--     status 供「入库管线」判断能否进入 indexing；
--     review_status 供「审核队列」筛选待办与统计，驳回后不再跟随 status 变化。
--     （例：驳回后重审通过 → status=approved、review_status=approved；
--       入库时 status 变为 indexed，而 review_status 恒为 approved 不变。）
--
-- 用法：
--   psql "$DATABASE_URL" -f sql/003_p3_db1_review.sql
--   或：python -m app.db.init_db            -- init_db 会按序执行 sql/00N_*.sql 全部文件
-- 幂等：
--   ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS；
--   CHECK 约束采用「先 DROP 再 ADD」的写法保证可重复执行。
-- 注意：
--   init_db.split_statements 按顶层分号切分语句，本文件**注释中不得出现半角分号**。
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. 新增审核结论列 review_status
-- ---------------------------------------------------------------------
-- 默认 'pending'（待审核），与 documents.status 的初值保持一致；
-- NOT NULL 保证「审核结论」永远有确定取值，避免前端出现空态歧义。
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS review_status VARCHAR(16) NOT NULL DEFAULT 'pending';


-- ---------------------------------------------------------------------
-- 2. 取值约束：仅允许 pending / approved / rejected
-- ---------------------------------------------------------------------
-- 先删后建，保证脚本可重复执行（PostgreSQL 不支持 ADD CONSTRAINT IF NOT EXISTS）。
ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS ck_documents_review_status;

ALTER TABLE documents
    ADD CONSTRAINT ck_documents_review_status
    CHECK (review_status IN ('pending', 'approved', 'rejected'));


-- ---------------------------------------------------------------------
-- 3. 存量数据回填（历史行由 DEFAULT 得到 'pending'，但已审核的行要按 status 纠正）
-- ---------------------------------------------------------------------
-- 说明：P3 之前不会产生 approved/rejected 数据，此段为「重复执行 / 手工改库」兜底：
--   status 已是 approved/rejected 但 review_status 仍为 pending 的行，按 status 对齐。
UPDATE documents
   SET review_status = CASE
                           WHEN status = 'rejected' THEN 'rejected'
                           WHEN status IN ('approved', 'indexing', 'indexed') THEN 'approved'
                           ELSE 'pending'
                       END
 WHERE review_status = 'pending'
   AND status IN ('approved', 'rejected', 'indexing', 'indexed');


-- ---------------------------------------------------------------------
-- 4. 索引：审核队列按「审核结论 + 提交时间」筛选排序
-- ---------------------------------------------------------------------
-- 管理员后台首页默认按 review_status='pending' + created_at 倒序取待办，
-- 该索引直接命中该查询模式（见 P3-B1 /admin/documents）。
CREATE INDEX IF NOT EXISTS idx_documents_review_status
    ON documents (review_status, created_at DESC);
