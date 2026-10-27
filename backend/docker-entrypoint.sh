#!/usr/bin/env bash
# 后端容器启动脚本（P1-Dep1）
# 流程：等待 PostgreSQL 就绪 → 执行建表（幂等）→ 启动 uvicorn
# 说明：建表脚本由 app.db.init_db 自动发现并执行 sql/ 下全部 00x_*.sql（已含 IF NOT EXISTS）。

set -euo pipefail

# ---------- 1. 解析数据库连接地址（从 DATABASE_URL 取 host/port） ----------
# 默认兜底值：postgresql+asyncpg://user:pass@postgres:5432/db
DB_URL="${DATABASE_URL:-postgresql+asyncpg://knowagent:knowagent@postgres:5432/knowagent}"
# 取 @ 之后、冒号/斜杠之前的主机名
DB_HOST=$(echo "$DB_URL" | sed -E 's#.*@([^:/]+).*#\1#')
# 取最后一个 :数字/ 之前的端口（asyncpg 串形如 ...@host:5432/db）
DB_PORT=$(echo "$DB_URL" | sed -E 's#.*:([0-9]+)/.*#\1#')
DB_PORT="${DB_PORT:-5432}"
# 透传给下面的 Python 探活子进程
export DB_HOST_WAIT="$DB_HOST"
export DB_PORT_WAIT="$DB_PORT"

echo "等待 PostgreSQL 就绪（${DB_HOST}:${DB_PORT}）..."
# until 循环：Python 探活连接成功（退出码 0）即结束等待
until python - <<'PY'
import socket, sys, os
host = os.environ["DB_HOST_WAIT"]
port = int(os.environ["DB_PORT_WAIT"])
s = socket.socket()
s.settimeout(2)
try:
    s.connect((host, port))
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
do
  echo "  PostgreSQL 尚未就绪，2 秒后重试..."
  sleep 2
done
echo "PostgreSQL 已就绪。"

# ---------- 2. 执行建表（幂等；SQL 内已含 IF NOT EXISTS） ----------
echo "执行 P1 建表脚本（users / conversations / messages / documents）..."
# 建表失败不阻断启动（例如表已存在），仅打印提示
python -m app.db.init_db || echo "⚠️ 建表未完全成功（可能表已存在，可忽略）"

# ---------- 3. 启动服务 ----------
# uvicorn 绑定 0.0.0.0:8000；是否热重载由环境变量 RELOAD 控制（Compose 中设为 false）。
echo "启动 uvicorn（0.0.0.0:8000）..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
