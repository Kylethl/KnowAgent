-- =====================================================================
-- 审计日志表（P7-B2：合规与私有化部署）
-- ---------------------------------------------------------------------
-- 对应《需求清单》P7 功能2「后端：数据不出域配置、审计日志」。
--
-- 为什么审计日志必须落在数据库而不是只写文件日志：
--   1. 可检索：合规检查要回答「谁在什么时候审核了哪份文档」「谁导出了审计记录」，
--      文本日志无法按动作/用户/资源做条件查询；
--   2. 可防篡改（相对）：文件日志随容器重建即丢失，数据库有独立备份链路
--      （见 P7-DB1 的 pg_dump 定时备份），审计证据能一起被保留；
--   3. 可展示：管理端「系统状态」页直接分页浏览，不必登录服务器。
--
-- 字段设计说明：
--   - username / role 为**快照**：用户改名或提权后，历史审计仍应反映当时身份，
--     不能 JOIN users 实时取值（否则历史记录会被悄悄改写）。
--   - user_id 仍保留外键（ON DELETE SET NULL）：既支持按用户聚合，又允许用户注销后
--     审计记录继续留档（合规要求审计不可因账号删除而消失）。
--   - resource_type + resource_id 用「类型 + 字符串 ID」而不是多个外键：
--     审计对象可能是文档、消息、备份文件、配置项……不可能为每种对象建列，
--     也不必因此引入多张关联表。资源被删后 ID 仍作为事实保留。
--   - detail 用 JSONB：不同动作关注的字段不同（驳回理由、备份大小、出站域名…），
--     半结构化最合适；同时便于 WHERE detail->>'host' = ... 这类查询。
--   - ip / user_agent / request_id：用于把审计事件与访问日志、Sentry 事件串联。
--   - 不存任何请求正文、密码、令牌（写入侧由 audit_service 做字段脱敏）。
--
-- 保留策略：默认 180 天（AUDIT_RETENTION_DAYS），由 Celery 定时任务清理。
-- 幂等：本脚本可重复执行（IF NOT EXISTS）。
-- =====================================================================

CREATE TABLE IF NOT EXISTS audit_logs (
    -- 主键：数据库侧生成，避免应用时钟不一致带来的排序问题
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 操作者：注销后置空（审计保留，身份快照在 username/role 里）
    user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    -- 操作者用户名快照（未登录的失败登录尝试也为空）
    username VARCHAR(64) NULL,
    -- 操作者角色快照：user / admin / anonymous
    role VARCHAR(16) NULL,

    -- 动作代码：点号分层，如 auth.login / document.review / ops.backup.trigger
    action VARCHAR(64) NOT NULL,
    -- 资源类型：document / message / user / backup / audit / config / llm
    resource_type VARCHAR(32) NULL,
    -- 资源标识（字符串以兼容 UUID 之外的资源，如备份文件名）
    resource_id VARCHAR(128) NULL,

    -- 结果：success（动作完成）/ failure（被拒绝或执行失败）
    result VARCHAR(16) NOT NULL DEFAULT 'success',

    -- 来源信息：定位「从哪台机器、哪个请求发起的」
    ip VARCHAR(64) NULL,
    user_agent VARCHAR(512) NULL,
    request_id VARCHAR(64) NULL,

    -- 动作细节（已脱敏）：如 {"note_len": 12, "previous": "pending", "current": "approved"}
    detail JSONB NULL,

    -- 发生时间：审计的核心排序与筛选字段
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 取值约束：与 ORM 模型（app/db/models.py 的 AuditLog）保持一致
    CONSTRAINT ck_audit_logs_result CHECK (result IN ('success', 'failure'))
);

-- 列表主索引：管理端按时间倒序分页浏览（最常见的查询）
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at
    ON audit_logs (created_at DESC);

-- 按动作筛选：合规检查常问「审核动作最近做了哪些」
CREATE INDEX IF NOT EXISTS idx_audit_logs_action_created
    ON audit_logs (action, created_at DESC);

-- 按用户追溯：调查某个账号的操作轨迹
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_created
    ON audit_logs (user_id, created_at DESC);

-- 按资源追溯：调查某份文档的全部操作（审核 / 重入库 / 下线）
CREATE INDEX IF NOT EXISTS idx_audit_logs_resource
    ON audit_logs (resource_type, resource_id);

-- 失败事件检索：安全审计关心「最近有哪些被拒绝的尝试」
CREATE INDEX IF NOT EXISTS idx_audit_logs_result_created
    ON audit_logs (result, created_at DESC)
    WHERE result = 'failure';
