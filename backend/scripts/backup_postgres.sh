#!/usr/bin/env bash
# =====================================================================
# PostgreSQL 备份脚本（P7-DB1 / P7-Dep1）
# ---------------------------------------------------------------------
# 用途：在不跑 Celery 的环境（单容器私有化交付、宿主机 crontab）下完成数据库备份。
#       与后端 services/backup_service.py 产出**同样的文件格式与 manifest**，
#       因此状态页（/ops/backups）能看到脚本产生的备份，反之亦然。
#
# 用法：
#   bash scripts/backup_postgres.sh                  # 备份一次
#   BACKUP_DIR=/data/backups bash scripts/backup_postgres.sh
#
# 环境变量（均可从 backend/.env 读取，脚本会自动加载同级 .env）：
#   DATABASE_URL          必填，形如 postgresql+asyncpg://user:pass@host:5432/db
#   BACKUP_DIR            备份目录（默认 data/backups）
#   BACKUP_PG_DUMP_BIN    pg_dump 路径（默认 pg_dump，可写绝对路径）
#   BACKUP_RETENTION_DAYS 保留天数（默认 14，0 表示不按天清理）
#   BACKUP_RETENTION_COUNT 保留个数（默认 30，0 表示不按个数清理）
#
# 定时执行（宿主机 crontab，每天 03:00）：
#   0 3 * * * cd /opt/knowagent/backend && /bin/bash scripts/backup_postgres.sh >> /var/log/knowagent-backup.log 2>&1
#
# 退出码：0 成功；1 配置/依赖错误；2 备份失败；3 校验失败
# =====================================================================
set -euo pipefail

# ---------- 定位目录并加载 .env ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${BACKEND_DIR}/.env" ]]; then
  # 只导出简单 KEY=VALUE 行，避免执行 .env 里的任意命令
  while IFS='=' read -r key value; do
    [[ -z "${key}" || "${key}" =~ ^# ]] && continue
    # 去掉可能存在的引号与首尾空白
    value="${value%\"}"; value="${value#\"}"
    export "${key}=${value}"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${BACKEND_DIR}/.env" || true)
fi

DATABASE_URL="${DATABASE_URL:-}"
if [[ -z "${DATABASE_URL}" ]]; then
  echo "[backup] 错误：未设置 DATABASE_URL" >&2
  exit 1
fi

PG_DUMP_BIN="${BACKUP_PG_DUMP_BIN:-pg_dump}"
if ! command -v "${PG_DUMP_BIN}" >/dev/null 2>&1; then
  echo "[backup] 错误：找不到 pg_dump（${PG_DUMP_BIN}）。Debian: apt install postgresql-client；macOS: brew install postgresql@16" >&2
  exit 1
fi

# ---------- 解析连接串（去掉 SQLAlchemy 的 +asyncpg 后缀） ----------
PG_URL="${DATABASE_URL/postgresql+asyncpg:\/\//postgresql://}"
PG_URL="${PG_URL/postgres+asyncpg:\/\//postgresql://}"

BACKUP_DIR="${BACKUP_DIR:-${BACKEND_DIR}/data/backups}"
mkdir -p "${BACKUP_DIR}"

RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
RETENTION_COUNT="${BACKUP_RETENTION_COUNT:-30}"

STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="knowagent-${STAMP}.sql.gz"
TARGET="${BACKUP_DIR}/${NAME}"
TMP_TARGET="${TARGET}.part"
START_TS="$(date +%s)"

echo "[backup] 开始：$(date '+%Y-%m-%d %H:%M:%S') → ${TARGET}"

# ---------- 执行导出 ----------
# --no-owner/--no-acl：恢复到不同角色/环境时无需先建同名角色
# 口令由 libpq 从 URL 解析；不落命令行参数（避免 ps 泄露）
if ! PGCONNECT_TIMEOUT=10 "${PG_DUMP_BIN}" \
      --dbname="${PG_URL}" \
      --no-owner --no-acl --format=plain \
      | gzip -6 > "${TMP_TARGET}"; then
  echo "[backup] 错误：pg_dump 执行失败" >&2
  rm -f "${TMP_TARGET}"
  exit 2
fi

# 空文件视为失败（避免留下「看起来有备份」的假文件）
if [[ ! -s "${TMP_TARGET}" ]]; then
  echo "[backup] 错误：备份文件为空" >&2
  rm -f "${TMP_TARGET}"
  exit 2
fi

# ---------- 完整性校验：gzip 能读通 + 末尾有 pg_dump 完成标记 ----------
if ! gzip -t "${TMP_TARGET}" 2>/dev/null; then
  echo "[backup] 错误：gzip 流损坏" >&2
  rm -f "${TMP_TARGET}"
  exit 3
fi
if ! gzip -dc "${TMP_TARGET}" | tail -n 5 | grep -q "PostgreSQL database dump complete"; then
  echo "[backup] 错误：缺少 pg_dump 完成标记（导出可能被中断）" >&2
  rm -f "${TMP_TARGET}"
  exit 3
fi

# 校验通过后再原子改名：目录里不会出现「半截但名字正常」的文件
mv "${TMP_TARGET}" "${TARGET}"

END_TS="$(date +%s)"
SIZE_BYTES="$(wc -c < "${TARGET}" | tr -d ' ')"
SHA256="$(shasum -a 256 "${TARGET}" | awk '{print $1}')"
ELAPSED_MS=$(( (END_TS - START_TS) * 1000 ))

# ---------- 写 manifest（与 Python 服务同结构，状态页可读） ----------
cat > "${TARGET}.json" <<JSON
{
  "name": "${NAME}",
  "size_bytes": ${SIZE_BYTES},
  "sha256": "${SHA256}",
  "created_at": "$(date -Iseconds)",
  "elapsed_ms": ${ELAPSED_MS},
  "trigger": "cli",
  "operator": "$(whoami)",
  "database": "$(echo "${PG_URL}" | sed -E 's#.*@([^/]+)/.*#\1#')",
  "verified": true
}
JSON

echo "[backup] 完成：${NAME}（$(( SIZE_BYTES / 1024 / 1024 ))MB，耗时 $(( ELAPSED_MS / 1000 ))s，sha256=${SHA256:0:12}…）"

# ---------- 保留策略清理（天数 + 个数双约束） ----------
removed=0
index=0
# 按修改时间倒序枚举；注意处理文件名含空格的情况（本脚本生成的文件名不含空格）
while IFS= read -r file; do
  [[ -z "${file}" ]] && continue
  index=$((index + 1))

  too_many=0
  if [[ "${RETENTION_COUNT}" -gt 0 && "${index}" -gt "${RETENTION_COUNT}" ]]; then
    too_many=1
  fi

  too_old=0
  if [[ "${RETENTION_DAYS}" -gt 0 ]]; then
    # -mtime +N 表示「修改时间早于 N 天前」
    if [[ -n "$(find "${file}" -mtime +"${RETENTION_DAYS}" -print -quit 2>/dev/null)" ]]; then
      too_old=1
    fi
  fi

  if [[ "${too_many}" -eq 1 || "${too_old}" -eq 1 ]]; then
    rm -f "${file}" "${file}.json"
    removed=$((removed + 1))
  fi
done < <(ls -1t "${BACKUP_DIR}"/knowagent-*.sql.gz 2>/dev/null || true)

[[ "${removed}" -gt 0 ]] && echo "[backup] 已清理 ${removed} 个过期备份"

exit 0
