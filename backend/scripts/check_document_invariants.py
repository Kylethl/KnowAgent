# 文档入库状态巡检（不变量检查）
# ---------------------------------------------------------------------
# 职责：用一组「必须永远为真」的断言扫描 documents / document_chunks，
#       把「测试没覆盖到、线上已经发生」的脏状态主动查出来，而不是等用户报障。
#
# 为什么需要它（真实事故驱动）：
#   用户 u_4ff45c 的《部署手册.md》状态卡在 indexing 约 16 小时。当时数据库里
#   同时存在 indexed_at 有值、chunk_count=3、status='indexing' 这种**互相矛盾**的组合——
#   一条 SQL 断言就能发现，但项目此前没有任何「数据不变量」检查，
#   于是问题只能靠用户在页面上发现「重新入库按钮是灰的」才暴露。
#
# 与测试的分工：
#   - 单测（tests/）验证代码逻辑，跑在**无数据库**的桩环境；
#   - 本脚本验证**真实数据**，天然可以发现「逻辑正确但历史数据脏」的情况
#     （例如开关切换、进程被杀留下的半截状态）。
#
# 用法（在 backend/ 目录下执行）：
#   python scripts/check_document_invariants.py
#       # 检查并要求全部通过；有违例时退出码 1（可直接用于 CI / 定时任务）
#   python scripts/check_document_invariants.py --warn-only
#       # 有违例也只提示、退出码 0（适合接入告警但不阻断发布）
#   python scripts/check_document_invariants.py --fix-stuck-indexing
#       # 把「僵死」的 indexing 文档回退为 approved，使其在后台可重新入库
#       # （只改状态，不动切片与向量；管理员仍需点一次「重新入库」）
#
# 设计原则：
#   1. 只读为主，唯一的写操作是 --fix-stuck-indexing，且必须显式开启；
#   2. 每条不变量都给出「为什么不能这样」与「怎么修」，而不是只报数字；
#   3. 不依赖 ORM（避免模型与库结构不一致带来的假警报），全部用原生 SQL。

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# 让脚本既能 `python scripts/check_document_invariants.py` 直接跑，
# 也能以模块方式跑：把 backend/ 根目录加入模块搜索路径。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.services import indexing_service  # noqa: E402
from sqlalchemy import text  # noqa: E402


@dataclass
class Violation:
    """一条不变量违例：包含现象、影响与修复建议。"""

    # 不变量名称（简短，便于在告警标题里识别）
    code: str
    # 违例数量
    count: int
    # 为什么这是问题（讲清后果，避免运维只看到数字不处理）
    impact: str
    # 修复建议（尽量给可执行动作）
    fix: str
    # 涉及的文档（最多 5 条，便于快速定位）
    samples: List[str] = field(default_factory=list)


# 每条不变量 = (SQL, code, impact, fix)
# SQL 必须是「返回违例行」的形式，行内最好带 filename 便于定位。
_CHECKS = [
    (
        "stuck_indexing",
        """
        SELECT d.filename, u.username,
               round(extract(epoch FROM (now() - d.updated_at)) / 60)::int AS idle_minutes
        FROM documents d LEFT JOIN users u ON u.id = d.user_id
        WHERE d.deleted_at IS NULL AND d.status = 'indexing'
          AND (d.updated_at IS NULL
               OR d.updated_at < now() - (CASE WHEN d.file_type IN ('image','audio','video')
                                               THEN CAST(:media_minutes AS integer) ELSE CAST(:text_minutes AS integer) END) * interval '1 minute')
        """,
        "入库任务已中断却仍停留在 indexing（无任何任务会把它推到终态）",
        "表现为后台只剩「预览」可用；修复前「重新入库」按钮会被禁用。"
        "点「重新入库」即可恢复；批量清理可加 --fix-stuck-indexing",
    ),
    (
        "contradictory_status",
        """
        SELECT d.filename, d.status, d.chunk_count, d.indexed_at
        FROM documents d
        WHERE d.deleted_at IS NULL
          AND (
            -- 已入库时间存在，状态却没到 indexed（本次事故的具体形态）
            (d.indexed_at IS NOT NULL AND d.status <> 'indexed')
            -- 有切片却仍是待审核/驳回
            OR (d.chunk_count > 0 AND d.status IN ('pending', 'rejected'))
            -- 标记已入库却没有切片
            OR (d.status = 'indexed' AND COALESCE(d.chunk_count, 0) = 0)
          )
        """,
        "状态与事实互相矛盾：界面显示的阶段不可信，前端据此做的门禁会误判",
        "按事实修正 status（有切片且已入库过的，应为 indexed）；"
        "若切片本身缺失，重新入库即可重建",
    ),
    (
        "chunk_count_mismatch",
        """
        SELECT d.filename, d.chunk_count AS declared, count(c.id) AS actual
        FROM documents d LEFT JOIN document_chunks c ON c.doc_id = d.id
        WHERE d.deleted_at IS NULL
        GROUP BY d.id, d.filename, d.chunk_count
        HAVING d.chunk_count <> count(c.id)
        """,
        "chunk_count 与实际切片数不一致：检索命中数、后台展示都会失真",
        "以实际切片为准回填 chunk_count；差异过大建议重新入库",
    ),
    (
        "orphan_chunks",
        """
        SELECT d.filename, count(c.id) AS chunks
        FROM document_chunks c JOIN documents d ON d.id = c.doc_id
        WHERE d.deleted_at IS NOT NULL
        GROUP BY d.id, d.filename
        """,
        "已软删除的文档仍留有切片：这些内容可能被检索到（违背「下线即不可检索」）",
        "删除这些切片（DELETE FROM document_chunks WHERE doc_id = ...），"
        "并失效检索缓存",
    ),
    (
        "null_embedding",
        """
        SELECT d.filename, count(c.id) AS null_vectors
        FROM document_chunks c JOIN documents d ON d.id = c.doc_id
        WHERE d.deleted_at IS NULL AND c.embedding IS NULL
        GROUP BY d.id, d.filename
        """,
        "切片没有向量：会被写入但永远检索不到（静默失效，最难排查）",
        "对相关文档执行重新入库",
    ),
]


async def _run_check(conn, sql: str, params: dict) -> List:
    """执行一条检查 SQL，返回原始行列表。"""
    result = await conn.execute(text(sql), params)
    return result.all()


async def collect_violations() -> List[Violation]:
    """逐条执行不变量检查，汇总违例。

    Returns:
        Violation 列表（只包含 count > 0 的项，按数量降序）。
    """
    settings = get_settings()
    params = {
        "text_minutes": max(int(settings.INDEXING_STALE_MINUTES), 1),
        "media_minutes": max(int(settings.MEDIA_INDEXING_STALE_MINUTES), 1),
    }
    violations: List[Violation] = []

    async with SessionLocal() as db:
        for code, sql, impact, fix in _CHECKS:
            rows = await _run_check(db, sql, params)
            if not rows:
                continue
            # 抽样展示：只取前 5 条，避免刷屏
            samples = [str(tuple(r)[:4])[:160] for r in rows[:5]]
            violations.append(
                Violation(code=code, count=len(rows), impact=impact, fix=fix, samples=samples)
            )

    violations.sort(key=lambda v: v.count, reverse=True)
    return violations


async def fix_stuck_indexing() -> int:
    """把僵死的 indexing 文档回退为 approved，使其可在后台重新入库。

    为什么回退而不是直接重跑：脚本可能在没有 worker 的环境里执行，
    真正入库应交给管理员在后台点一次（有可见反馈）。这里只解开状态锁。

    Returns:
        受影响的文档条数。
    """
    settings = get_settings()
    async with SessionLocal() as db:
        result = await db.execute(
            text(
                """
                UPDATE documents
                SET status = 'approved',
                    -- 清空上次成功入库时间：本脚本的「矛盾」不变量要求
                    -- indexed_at 非空 ⇔ status='indexed'，不清会立刻再报一次，形成假修复
                    indexed_at = NULL,
                    -- 说明原因，管理员在后台能看到「为什么它回到待入库」
                    last_index_error = '上次入库被中断（长时间停留在入库中），已自动回退，请重新入库',
                    updated_at = now()
                WHERE deleted_at IS NULL AND status = 'indexing'
                  AND (updated_at IS NULL
                       OR updated_at < now() - (CASE WHEN file_type IN ('image','audio','video')
                                                     THEN CAST(:media_minutes AS integer) ELSE CAST(:text_minutes AS integer) END) * interval '1 minute')
                """
            ),
            {
                "text_minutes": max(int(settings.INDEXING_STALE_MINUTES), 1),
                "media_minutes": max(int(settings.MEDIA_INDEXING_STALE_MINUTES), 1),
            },
        )
        await db.commit()
        return result.rowcount or 0


def _print_report(violations: List[Violation], total_checked: int) -> None:
    """把检查结果打印成可读报告（全部通过时给一行 OK，便于 CI 日志阅读）。"""
    if not violations:
        print(f"✅ 全部 {total_checked} 项不变量通过，未发现异常数据")
        return

    print(f"❌ 发现 {len(violations)} 类异常（共 {sum(v.count for v in violations)} 条记录）\n")
    for idx, v in enumerate(violations, start=1):
        print(f"{idx}. [{v.code}] {v.count} 条 —— {v.impact}")
        print(f"   修复建议：{v.fix}")
        for sample in v.samples:
            print(f"   · {sample}")
        print()


async def main() -> int:
    """入口：解析参数 → 检查 → 可选修复 → 输出报告。

    Returns:
        进程退出码：0 表示通过（或 --warn-only），1 表示存在违例。
    """
    parser = argparse.ArgumentParser(description="文档入库状态不变量巡检")
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="只提示不阻断：有违例也返回退出码 0",
    )
    parser.add_argument(
        "--fix-stuck-indexing",
        action="store_true",
        help="把僵死的 indexing 文档回退为 approved（只改状态，不重跑入库）",
    )
    args = parser.parse_args()

    if args.fix_stuck_indexing:
        fixed = await fix_stuck_indexing()
        print(f"已回退 {fixed} 篇卡在入库中的文档为「已审核待入库」，请在后台点「重新入库」\n")

    violations = await collect_violations()
    _print_report(violations, total_checked=len(_CHECKS))

    if not violations:
        return 0
    return 0 if args.warn_only else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
