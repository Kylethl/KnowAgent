# P1-DB1 / P1-DB2 建表入口（可独立运行）
# ---------------------------------------------------------------------
# 职责：读取 backend/sql/ 下全部建表脚本（按文件名排序）并执行，
#       完成 users / conversations / messages（P1-DB1）与 documents（P1-DB2）建表。
#       规范 SQL 以 backend/sql/NNN_*.sql 为唯一源头，本工具仅负责「执行」。
#
# 用法：
#   python -m app.db.init_db            # 执行 sql/ 下全部建表脚本
#   python -m app.db.init_db --check    # 仅探活数据库连通性，不建表
#   python -m app.db.init_db --schema <path>  # 仅执行指定单个 SQL 文件

import argparse
from pathlib import Path

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.engine import engine, ping

logger = get_logger(__name__)

# 规范 SQL 目录：backend/sql/（__file__ = backend/app/db/init_db.py → parents[2] = backend）
SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def discover_schema_files() -> list[Path]:
    """按文件名排序收集 sql/ 下全部建表脚本（00x_*.sql），保证执行顺序。

    Returns:
        排好序的 SQL 文件路径列表；目录不存在时返回空列表。
    """
    if not SQL_DIR.exists():
        return []
    # 形如 001_p1_db1_schema.sql、002_p1_db2_documents.sql，按名称字典序即按阶段序
    return sorted(SQL_DIR.glob("*.sql"))


def split_statements(sql: str) -> list[str]:
    """把 SQL 文本按顶层分号切分为多条独立语句。

    说明：本规范文件不含字符串内分号或 PL/pgSQL 代码块，
          因此简单的分号切分即可；更复杂的脚本应改用 SQL 解析器。
    """
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


async def init_db(schema_path: Path | None = None) -> None:
    """按规范建表：执行 sql/ 下全部脚本（或指定单文件），幂等（SQL 内 IF NOT EXISTS）。

    Args:
        schema_path: 指定单个 SQL 路径时只执行该文件；缺省则执行 discover_schema_files() 全部。
    """
    # 决定要执行的 SQL 文件列表
    paths = [schema_path] if schema_path else discover_schema_files()
    if not paths:
        raise FileNotFoundError(f"未在 {SQL_DIR} 找到任何建表 SQL 文件")

    total = 0
    # 在一个事务内顺序执行所有文件的所有 DDL；任一条失败整体回滚，便于排查
    async with engine.begin() as conn:
        for path in paths:
            if not path.exists():
                raise FileNotFoundError(f"找不到建表 SQL 文件：{path}")
            # 读取并切分该文件的语句
            sql = path.read_text(encoding="utf-8")
            statements = split_statements(sql)
            for stmt in statements:
                # 仅打印前 80 字符作为日志，避免把完整 DDL 刷屏
                logger.info("执行 DDL[%s]：%s", path.name, stmt[:80].replace("\n", " "))
                await conn.execute(text(stmt))
            total += len(statements)

    logger.info(
        "建表完成（共 %d 条语句，%d 个文件）：%s",
        total,
        len(paths),
        ", ".join(p.name for p in paths),
    )


def _run() -> None:
    """命令行入口：解析参数并执行「建表」或「探活」。"""
    import asyncio

    parser = argparse.ArgumentParser(description="P1 建表工具（执行 sql/ 下全部脚本）")
    parser.add_argument(
        "--check", action="store_true", help="仅探活数据库连通性，不建表"
    )
    parser.add_argument(
        "--schema", type=str, default=None, help="仅执行指定单个 SQL 文件路径"
    )
    args = parser.parse_args()

    if args.check:
        # 仅探活：打印结果并退出（0=成功，1=失败）
        ok = asyncio.run(ping())
        print("数据库连通性：", "OK" if ok else "FAIL")
        raise SystemExit(0 if ok else 1)

    # 执行建表（默认全部脚本；--schema 则仅指定文件）
    schema = Path(args.schema) if args.schema else None
    asyncio.run(init_db(schema))
    print("P1 建表脚本执行成功。")


if __name__ == "__main__":
    _run()
