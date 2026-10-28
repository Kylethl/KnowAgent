# RAG 相似度阈值标定工具（P4 功能1 运维辅助）
# ---------------------------------------------------------------------
# 用途：`RAG_SIMILARITY_THRESHOLD` 是**模型相关**的参数——
#       不同 embedding 模型对「无关文本」也会给出不同水平的余弦相似度
#       （本项目实测：当前模型无关中文普遍落在 0.15~0.45，因此阈值取 0.30）。
#       换模型后沿用旧阈值会出两类故障：
#         - 阈值偏低 → 无关切片被塞进 Prompt，模型被迫「拿着无关资料作答」；
#         - 阈值偏高 → 有相关文档却全被过滤，永远回答「知识库未找到」。
#
# 标定思路（不依赖人工标注，也不自欺欺人）：
#   阈值只需要卡住「无关」，因此**以下界分布为主**：
#     1. 无关样本：跨文档切片两两比对（同一文档内切片主题相近，不能当无关样本，
#        否则会把下界算高）。取其次数的 p95 作为「无关噪声的上界」，
#        建议阈值 = 该 p95（可加小余量），保证绝大多数无关切片被挡在门外；
#     2. 真实问句校验（可选 --queries）：给出真实问题时，量出它对本库的最高相似度，
#        如果建议阈值 > 该值，说明阈值会把这类真实问题也过滤掉，需要人工下调。
#        —— 这是唯一能反映「相关侧」分布的可靠数据，来自真实提问而非合成样本。
#     3. 健康检查：切片正文前缀与其全文的相似度应接近 1.0。
#        若明显偏低，说明向量接口降级（mock）或模型异常，此时任何标定都无意义。
#
# 代价：只调用 embedding 接口 O(样本数 + 问句数) 次，按批提交；
#       不修改数据库、不修改配置（阈值属需人工确认的运行参数）。
#
# 用法（在 backend/ 目录执行）：
#   python scripts/calibrate_rag_threshold.py
#   python scripts/calibrate_rag_threshold.py --samples 40
#   python scripts/calibrate_rag_threshold.py --queries "支持哪些文档格式？;怎么部署"
#   python scripts/calibrate_rag_threshold.py --queries-file queries.txt --apply-hint

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

# 让脚本既能 `python scripts/xxx.py` 直接跑，也能以 `-m scripts.xxx` 方式跑
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402 —— 必须在 sys.path 调整后导入项目模块

from app.config import get_settings  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.db.models import Document, DocumentChunk  # noqa: E402
from app.services.embedding import embed_texts  # noqa: E402

# 健康检查用的正文前缀长度（模拟「同一段内容的短版本」，相似度应接近 1.0）
_PREFIX_CHARS = 60
# 建议阈值的下限：即便实测下界很低，也不要低于此值——
# 中文短问句与任意文本的相似度天然有 0.1~0.2 的底噪，设太低等于不设阈值。
_MIN_SUGGESTED = 0.25


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """计算两个向量的余弦相似度（与 pgvector 的 `<=>` 语义一致：值越大越相似）。

    Args:
        a: 向量 A。
        b: 向量 B。

    Returns:
        相似度（-1~1）；任一向量为零向量时返回 0.0（避免除零）。
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def percentile(values: List[float], p: float) -> float:
    """计算分位数（线性插值，p 取 0~1）。

    Args:
        values: 样本值列表（函数内部对副本排序，不修改入参）。
        p: 目标分位（0.95 表示 95 分位）。

    Returns:
        分位数值；空列表返回 0.0。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = p * (len(ordered) - 1)
    low, high = int(math.floor(pos)), int(math.ceil(pos))
    if low == high:
        return ordered[low]
    weight = pos - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


async def load_sample_chunks(limit: int) -> List[Tuple[str, str]]:
    """从库里读取一批「已入库」文档的切片用于标定。

    Args:
        limit: 采样上限。

    Returns:
        [(doc_id, content)]；库里没有可用切片时返回空列表。
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(DocumentChunk.doc_id, DocumentChunk.content)
                .join(Document, Document.id == DocumentChunk.doc_id)
                # 只取真正参与检索的文档，保证标定结果与线上行为一致
                .where(Document.status == "indexed", Document.deleted_at.is_(None))
                .limit(limit)
            )
        ).all()
    return [(str(r.doc_id), (r.content or "").strip()) for r in rows if (r.content or "").strip()]


def cross_doc_similarities(
    chunks: List[Tuple[str, str]], vectors: List[List[float]]
) -> List[float]:
    """计算「跨文档切片对」的相似度，作为无关样本分布。

    为什么限定跨文档：同一篇文档内的相邻切片往往共享主题与术语，
    它们的相似度天然偏高，混进来会把「无关下界」推高、导致阈值偏严。

    Args:
        chunks: [(doc_id, content)]，与 vectors 一一对应。
        vectors: 各切片的向量。

    Returns:
        相似度列表（样本数不足时可能为空）。
    """
    rng = random.Random(20261028)  # 固定种子：同批数据结果可复现
    pairs: List[float] = []
    # 随机抽对，最多抽取切片数 3 倍的样本量，兼顾覆盖与耗时（纯本地计算，无接口调用）
    attempts = 0
    target = len(chunks) * 3
    while len(pairs) < target and attempts < target * 20:
        attempts += 1
        if len(chunks) < 2:
            break
        i, j = rng.sample(range(len(chunks)), 2)
        if chunks[i][0] == chunks[j][0]:
            continue  # 同文档：跳过
        pairs.append(cosine(vectors[i], vectors[j]))
    return pairs


def describe(name: str, values: List[float]) -> None:
    """打印一组相似度的统计摘要。

    Args:
        name: 组名。
        values: 相似度列表。
    """
    if not values:
        print(f"{name}：无样本")
        return
    mean = sum(values) / len(values)
    print(
        f"{name}（{len(values)} 对）：mean={mean:.3f}  p50={percentile(values, 0.5):.3f}  "
        f"p95={percentile(values, 0.95):.3f}  max={max(values):.3f}  min={min(values):.3f}"
    )


def parse_queries(raw: str | None, file_path: str | None) -> List[str]:
    """解析命令行传入的真实问句（分号/换行分隔，或从文件读取）。

    Args:
        raw: --queries 的原始字符串。
        file_path: --queries-file 路径（一行一个问句）。

    Returns:
        问句列表（去空、去重，保持原顺序）。
    """
    items: List[str] = []
    if raw:
        # 半角/全角分号与换行都作为分隔符，避免中文输入法下分号失效
        for part in raw.replace("；", ";").split(";"):
            items.append(part.strip())
    if file_path:
        text = Path(file_path).read_text(encoding="utf-8")
        items.extend(line.strip() for line in text.splitlines())
    # 去空 + 去重（保留首次出现顺序）
    seen: set[str] = set()
    result: List[str] = []
    for q in items:
        if q and q not in seen:
            seen.add(q)
            result.append(q)
    return result


async def run(samples: int, queries: List[str], apply_hint: bool) -> int:
    """执行标定主流程。

    Args:
        samples: 采样切片数。
        queries: 真实问句列表（可为空）。
        apply_hint: 是否额外打印 .env 配置写法。

    Returns:
        进程退出码（0 成功；1 数据或配置不足）。
    """
    settings = get_settings()
    print("=" * 72)
    print(f"当前配置：model={settings.QWEN_EMBEDDING_MODEL}  dim={settings.EMBEDDING_DIM}")
    print(f"当前阈值：RAG_SIMILARITY_THRESHOLD={settings.RAG_SIMILARITY_THRESHOLD}")
    if not settings.is_qwen_configured:
        # mock 向量不具备语义，标定毫无意义，直接拒绝以免给出误导结论
        print("❌ QWEN_API_KEY 未配置（当前为 mock 向量），无法标定阈值。请先填入真实 Key。")
        return 1

    chunks = await load_sample_chunks(samples)
    if len(chunks) < 4:
        print(f"❌ 可用切片仅 {len(chunks)} 条（需 ≥4），请先入库更多文档后再标定。")
        return 1
    print(f"采样切片：{len(chunks)} 条（来自已入库文档）")

    # ---------- 1) 一次性向量化：切片正文 + 前缀（健康检查用） ----------
    print("正在调用 embedding 接口（可能耗时数十秒）...")
    texts = [content for _, content in chunks] + [
        content[:_PREFIX_CHARS] for _, content in chunks
    ]
    vectors = await embed_texts(texts)
    chunk_vectors = vectors[: len(chunks)]
    prefix_vectors = vectors[len(chunks) :]

    # ---------- 2) 健康检查：前缀 vs 全文应接近 1.0 ----------
    health = [cosine(p, c) for p, c in zip(prefix_vectors, chunk_vectors)]
    health_mean = sum(health) / len(health)
    print("-" * 72)
    print(f"向量服务健康检查（正文前缀 vs 全文）：mean={health_mean:.3f}（期望 ≈1.0）")
    if health_mean < 0.90:
        # 只有「同一段内容的两个版本」都算不出高相似度时，才说明向量不可信
        print(
            "❌ 相似度异常偏低：向量可能来自 mock 模式、或接口返回了错误数据。\n"
            "   请先确认 QWEN_API_KEY / 模型名正确，再重新入库后再标定。"
        )
        return 1

    # ---------- 3) 无关样本分布（纯本地计算） ----------
    unrelated = cross_doc_similarities(chunks, chunk_vectors)
    describe("无关切片对（跨文档）", unrelated)
    if not unrelated:
        print("❌ 样本全来自同一文档，无法构造无关样本；请入库不同主题的文档后重试。")
        return 1

    noise_upper = percentile(unrelated, 0.95)
    suggested = round(max(noise_upper, _MIN_SUGGESTED), 2)
    print(f"建议阈值 = {suggested}（无关噪声 p95={noise_upper:.3f}，下限保护 {_MIN_SUGGESTED}）")

    # ---------- 4) 真实问句校验（可选） ----------
    max_hit = None
    if queries:
        print("-" * 72)
        print("真实问句校验（对本库的最高相似度；低于阈值即会被判为未命中）：")
        q_vectors = await embed_texts(queries)
        hits: List[float] = []
        for question, qv in zip(queries, q_vectors):
            sims = [cosine(qv, cv) for cv in chunk_vectors]
            top = max(sims)
            hits.append(top)
            flag = "✅" if top >= suggested else "⚠️ 会被过滤"
            print(f"  {top:.3f}  {flag}  {question}")
        max_hit = max(hits)
        # 若真实问句的最高相似度低于建议阈值，说明阈值偏严（相关侧被误杀）
        if max_hit < suggested:
            print(
                f"⚠️ 真实问句最高仅 {max_hit:.3f} < 建议阈值 {suggested}：\n"
                f"   说明该阈值会把真实问题也挡在门外，实际启用建议取 min({suggested}, "
                f"{round(max_hit * 0.9, 2)}) 或更低，并结合召回效果回归确认。"
            )
            suggested = round(min(suggested, max(max_hit * 0.9, _MIN_SUGGESTED)), 2)
            print(f"   修正后建议阈值：{suggested}")

    # ---------- 5) 与当前配置对比 ----------
    print("-" * 72)
    current = settings.RAG_SIMILARITY_THRESHOLD
    if current < noise_upper:
        print(
            f"⚠️ 当前阈值 {current} 低于无关噪声 p95={noise_upper:.3f}："
            "约 5% 以上的无关切片会被注入 Prompt，存在答非所问风险。"
        )
    elif max_hit is not None and current > max_hit:
        print(f"⚠️ 当前阈值 {current} 高于真实问句最高相似度 {max_hit:.3f}：会漏掉本应命中的资料。")
    else:
        print(f"✅ 当前阈值 {current} 与实测分布相容。")

    if apply_hint:
        print("-" * 72)
        print("如需调整，在 backend/.env 中加入（改完需重启后端）：")
        print(f"RAG_SIMILARITY_THRESHOLD={suggested}")
    return 0


def main() -> None:
    """命令行入口：解析参数并执行标定。"""
    parser = argparse.ArgumentParser(
        description="RAG 相似度阈值标定（实测无关样本分布 + 真实问句校验）"
    )
    parser.add_argument(
        "--samples", type=int, default=24, help="采样切片数（默认 24，越大越准也越慢）"
    )
    parser.add_argument(
        "--queries",
        default=None,
        help="真实问句，分号分隔（如 \"支持哪些格式？;怎么部署\"），用于校验阈值是否会漏召回",
    )
    parser.add_argument(
        "--queries-file", default=None, help="真实问句文件，一行一个"
    )
    parser.add_argument("--apply-hint", action="store_true", help="额外打印 .env 配置写法")
    args = parser.parse_args()

    queries = parse_queries(args.queries, args.queries_file)
    raise SystemExit(asyncio.run(run(args.samples, queries, args.apply_hint)))


if __name__ == "__main__":
    main()
