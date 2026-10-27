-- PostgreSQL 初始化脚本：首次创建数据卷时自动启用 pgvector 扩展
-- ---------------------------------------------------------------------
-- 生效时机：仅当 PGDATA 为空（全新数据卷）时，postgres 镜像才会执行
--           /docker-entrypoint-initdb.d/*.sql；已有数据卷不会重复执行，
--           因此本脚本是幂等且安全的（对存量库无副作用）。
--
-- 对应需求：《需求清单》P3 功能2 / 任务清单 P3-Dep1
--           「PostgreSQL 安装 pgvector 扩展」——向量列与相似度检索的前置条件。
--
-- 若迁移存量库（数据卷已存在），请手动执行一次：
--   docker compose -f docker/docker-compose.dev.yml exec postgres \
--     psql -U knowagent -d knowagent -c "CREATE EXTENSION IF NOT EXISTS vector;"

CREATE EXTENSION IF NOT EXISTS vector;
