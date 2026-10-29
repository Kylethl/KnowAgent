#!/usr/bin/env bash
# =====================================================================
# PostgreSQL 恢复脚本（P7-DB1）—— ⚠️ 高危操作，仅限人工执行
# ---------------------------------------------------------------------
# 为什么恢复不提供 HTTP 接口：
#   恢复会**覆盖整个数据库**，一旦被误调用（或被越权调用）就是不可逆的数据灾难。
#   因此恢复只能在服务器上、由有权限的人显式执行，并强制二次确认。
#
# 用法：
#   # 1) 先看有哪些备份（也可在后端「系统状态」页看到同样的列表）
#   ls -lh data/backups/
#
#   # 2) 恢复到**测试库**验证备份可用（强烈建议先在测试库演练）
#   PGDATABASE_URL='postgresql://user:pass@localhost:5432/knowagent_restore' \
#     bash scripts/restore_postgres.sh data/backups/knowagent-20261028-030000.sql.gz --yes
#
#   # 3) 恢复生产库（会清空现有数据！先停后端与 worker，避免写入竞争）
#   bash scripts/restore_postgres.sh data/backups/knowagent-20261028-030000.sql.gz --yes
#
# 安全设计：
#   1. 必须带 --yes，否则只打印将要执行的动作后退出（防止手滑）；
#   2. 恢复前自动做一次「当前库的备份」（unless --no-pre-backup），
#      万一恢复的是错的包，还能退回原状；
#   3. 恢复前校验目标文件（gzip 完整性 + pg_dump 完成标记）；
#   4. 使用 --single-transaction：要么全部成功，要么全部回滚，不留半套数据。
#
# 环境变量：
#   DATABASE_URL  目标库连接串（默认读 backend/.env）
#   PG_RESTORE_BIN / PSQL_BIN  可执行文件路径（默认走 PATH）
#
# 退出码：0 成功；1 参数/配置错误；2 文件校验失败；3 恢复失败
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

BACKUP_FILE="${1:-}"
CONFIRMED="no"
SKIP_PRE_BACKUP="no"
for arg in "${@:2}"; do
  case "${arg}" in
    --yes) CONFIRMED="yes" ;;
    --no-pre-backup) SKIP_PRE_BACKUP="yes" ;;
    *) echo "[restore] 未知参数：${arg}" >&2; usage ;;
  esac
done

[[ -z "${BACKUP_FILE}" || "${BACKUP_FILE}" == "-h" || "${BACKUP_FILE}" == "--help" ]] && usage
[[ -f "${BACKUP_FILE}" ]] || { echo "[restore] 错误：文件不存在：${BACKUP_FILE}" >&2; exit 1; }

# ---------- 加载 .env（只解析 KEY=VALUE，不执行任意命令） ----------
if [[ -f "${BACKEND_DIR}/.env" ]]; then
  while IFS='=' read -r key value; do
    [[ -z "${key}" || "${key}" =~ ^# ]] && continue
    value="${value%\"}"; value="${value#\"}"
    export "${key}=${value}"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${BACKEND_DIR}/.env" || true)
fi

# 允许用 PGDATABASE_URL 覆盖（便于「恢复到测试库」演练）
PG_URL="${PGDATABASE_URL:-${DATABASE_URL:-}}"
[[ -z "${PG_URL}" ]] && { echo "[restore] 错误：未设置 DATABASE_URL 或 PGDATABASE_URL" >&2; exit 1; }
PG_URL="${PG_URL/postgresql+asyncpg:\/\//postgresql://}"
PG_URL="${PG_URL/postgres+asyncpg:\/\//postgresql://}"

PSQL_BIN="${PSQL_BIN:-psql}"
command -v "${PSQL_BIN}" >/dev/null 2>&1 || {
  echo "[restore] 错误：找不到 psql（${PSQL_BIN}）。Debian: apt install postgresql-client" >&2
  exit 1
}

echo "=============================================================="
echo " ⚠️  数据库恢复（覆盖现有数据，不可撤销）"
echo " 备份文件：${BACKUP_FILE}"
echo " 目标库  ：$(echo "${PG_URL}" | sed -E 's#//[^@]*@#//***@#')"
echo "=============================================================="

if [[ "${CONFIRMED}" != "yes" ]]; then
  echo "[restore] 未确认，已退出。确认执行请追加 --yes"
  exit 1
fi

# ---------- 文件校验（宁可拒绝，也不要恢复半套数据） ----------
echo "[restore] 校验备份文件…"
gzip -t "${BACKUP_FILE}" 2>/dev/null || { echo "[restore] 错误：gzip 流损坏" >&2; exit 2; }
gzip -dc "${BACKUP_FILE}" | tail -n 5 | grep -q "PostgreSQL database dump complete" || {
  echo "[restore] 错误：缺少 pg_dump 完成标记，可能是不完整的备份" >&2
  exit 2
}

# ---------- 恢复前先备份当前库（除非显式跳过） ----------
if [[ "${SKIP_PRE_BACKUP}" != "yes" ]]; then
  echo "[restore] 先备份当前库（安全兜底）…"
  bash "${SCRIPT_DIR}/backup_postgres.sh" || {
    echo "[restore] 错误：恢复前备份失败，已中止（如需强制恢复请加 --no-pre-backup）" >&2
    exit 3
  }
fi

# ---------- 执行恢复 ----------
# --single-transaction：整包一个事务，中途失败自动回滚，不会留半套表
# ON_ERROR_STOP=1     ：遇错立即停止，不继续执行后续语句
# 说明：备份带 --no-owner/--no-acl，恢复到其它角色时不会因缺角色报错
echo "[restore] 开始恢复：$(date '+%Y-%m-%d %H:%M:%S')"
if ! gzip -dc "${BACKUP_FILE}" | PGCONNECT_TIMEOUT=10 \
      PGOPTIONS='--client-min-messages=warning' \
      "${PSQL_BIN}" --dbname="${PG_URL}" --single-transaction -v ON_ERROR_STOP=1 -q; then
  echo "[restore] 错误：恢复失败（事务已回滚，原数据未受影响）" >&2
  exit 3
fi

echo "[restore] 恢复完成：$(date '+%Y-%m-%d %H:%M:%S')"
echo "[restore] 后续：确认后端与 worker 已启动，并访问 /health/ready 与 /ops/status 验证"
