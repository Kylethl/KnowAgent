#!/usr/bin/env python
"""RAG 回答契约评测（真实模型 + 真实检索）。

为什么需要它（这是本次缺陷补上的那一层测试）：
    单元测试把 LLM mock 掉，只能验证「prompt 里写了什么」，验证不了「模型会怎么答」。
    而线上缺陷恰好发生在后者：零命中时模型仍给通用知识标上 [n]，
    生成了「徽标说未命中、正文却带引用」的自相矛盾回答。
    这类问题的唯一可靠验证方式是**真实调用 + 结构断言**：
    不比对具体措辞（模型每次说法都不同），只检查「允许/禁止出现的形态」。

判定的四类断言：
    1. 检索模式符合预期（no_hit / rag）——同时检验阈值标定是否漂移；
    2. 引用一致：正文里的每个编号都能在引用列表里找到（citation_guard）；
    3. 禁止项：如「已入库」「出自知识库」「知识库中未找到」（正例不该出现）；
    4. 必需项：如零命中时必须说明「未找到」。

用法（不会进 CI，按需/夜间跑）：
    cd backend
    python scripts/run_rag_eval.py              # 跑全部用例
    python scripts/run_rag_eval.py --id no_hit  # 只跑 id 含 no_hit 的用例
    python scripts/run_rag_eval.py --dry-run    # 只跑检索，不调模型（零成本）

退出码：0 全部通过；1 有用例失败（便于挂到 CI/定时任务上）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# 允许 `python scripts/run_rag_eval.py` 直接运行：把 backend 根目录塞进 sys.path
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.db.engine import SessionLocal  # noqa: E402 —— 必须在 sys.path 调整之后导入
from app.services.chat_chain import stream_answer  # noqa: E402
from app.services.citation_guard import check_citation_consistency  # noqa: E402
from app.services.retriever import retrieve_chunks  # noqa: E402

# 用例文件与脚本同目录，便于整包拷贝
CASES_PATH = Path(__file__).resolve().parent / "rag_eval_cases.json"


def load_cases(id_filter: str | None = None) -> List[Dict[str, Any]]:
    """读取评测集，并按 id 关键字过滤。

    Args:
        id_filter: 只保留 id 中包含该子串的用例；None 表示全部。

    Returns:
        用例列表。
    """
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = payload.get("cases", [])
    if id_filter:
        cases = [c for c in cases if id_filter in c.get("id", "")]
    return cases


def _matches(text: str, patterns: List[str]) -> List[str]:
    """返回命中的模式列表（空列表表示全部未命中）。"""
    return [p for p in patterns if p and re.search(p, text)]


async def run_case(case: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """跑一条用例，返回结构化结果（不抛异常，失败信息写进 result）。

    Args:
        case: 单条用例（见 rag_eval_cases.json）。
        dry_run: 只做检索、不调模型（用于零成本校准阈值）。

    Returns:
        {"id", "ok", "failures": [...], "mode", "answer", "elapsed_ms"}
    """
    question = case["question"]
    expect = case.get("expect", "rag")
    failures: List[str] = []
    started = time.perf_counter()

    # 1) 真实检索（与线上一致：缓存、阈值、重排全部生效）
    async with SessionLocal() as db:
        retrieval = await retrieve_chunks(question, db)

    # 2) 断言检索模式：no_hit 用例若被检索到资料，说明阈值/切分漂移了
    if retrieval.mode != expect:
        failures.append(f"检索模式不符：期望 {expect}，实际 {retrieval.mode}")

    if dry_run:
        return {
            "id": case["id"],
            "ok": not failures,
            "failures": failures,
            "mode": retrieval.mode,
            "answer": "(dry-run，未调用模型)",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    # 3) 真实生成：严格按线上方式调用（零命中传空串，走通用链 + NO_HIT_RULES）
    parts: List[str] = []
    async for chunk in stream_answer(
        question, history=[], meta={}, memory_enabled=False, context=retrieval.context
    ):
        parts.append(chunk)
    answer = "".join(parts)

    # 4) 引用一致性：正文编号必须都能在引用列表里找到
    #    这里直接复用线上挑选引用的函数，保证判定口径与线上完全一致
    from app.services.retriever import select_citations

    citations = select_citations(retrieval.chunks, answer)
    consistency = check_citation_consistency(answer, citations)
    if consistency.dangling:
        failures.append(f"幻觉引用：正文编号 {consistency.dangling} 不在引用列表中")

    # 5) 禁止项 / 必需项（用正则，允许措辞微调）
    hit_forbidden = _matches(answer, case.get("forbid_patterns", []))
    if hit_forbidden:
        failures.append(f"出现禁止内容：{hit_forbidden}")
    missing_required = [
        p for p in case.get("require_patterns", []) if not re.search(p, answer)
    ]
    if missing_required:
        failures.append(f"缺少必需内容：{missing_required}")

    return {
        "id": case["id"],
        "ok": not failures,
        "failures": failures,
        "mode": retrieval.mode,
        "answer": answer,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


async def main() -> int:
    """入口：解析参数 → 顺序跑用例 → 打印汇总 → 返回退出码。"""
    parser = argparse.ArgumentParser(description="RAG 回答契约评测")
    parser.add_argument("--id", dest="id_filter", default=None, help="只跑 id 含该子串的用例")
    parser.add_argument("--dry-run", action="store_true", help="只跑检索，不调用模型")
    parser.add_argument("--verbose", action="store_true", help="打印每条用例的回答全文")
    args = parser.parse_args()

    cases = load_cases(args.id_filter)
    if not cases:
        print("没有匹配的用例，请检查 --id 参数")
        return 1

    print(f"开始评测：{len(cases)} 条用例（dry_run={args.dry_run}）\n")
    results = []
    # 顺序执行：并发调模型容易触发上游限流，反而污染结果
    for case in cases:
        result = await run_case(case, args.dry_run)
        results.append(result)
        flag = "PASS" if result["ok"] else "FAIL"
        print(f"[{flag}] {result['id']:<24} mode={result['mode']:<8} {result['elapsed_ms']}ms")
        for failure in result["failures"]:
            print(f"       └─ {failure}")
        if args.verbose and result["answer"]:
            print(f"       └─ 回答片段：{result['answer'][:160].replace(chr(10), ' ')}")

    passed = sum(1 for r in results if r["ok"])
    print(f"\n汇总：{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
