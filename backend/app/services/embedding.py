# 文本向量化客户端（P3-A1：调用千问 Embedding）
# ---------------------------------------------------------------------
# 职责：把文本切片批量转换为向量，供 pgvector 入库（P3-B2）与检索召回（P4）使用。
#
# 对应《需求清单》：
#   - 知识库 Embedding 默认 qwen3-vl-embedding（支持图文多模态向量，P5 复用本模块）
#
# 设计要点：
#   1. 双协议支持（由 config.QWEN_EMBEDDING_API_MODE 控制，默认 auto 自动识别）：
#      (a) OpenAI 兼容协议（阿里云百炼 compatible-mode 网关、本地 vLLM 等）：
#          POST {BASE_URL}/embeddings，请求体 {"model":..,"input":[..],"dimensions":..}，
#          响应取 data[*].embedding。本项目本地开发即走此协议。
#      (b) DashScope 原生多模态向量接口（qwen3-vl-embedding 默认 BASE_URL）：
#          POST {BASE_URL}（已是完整 endpoint），请求体
#          {"model":..,"input":{"contents":[{"text":..}]},"parameters":{"dimension":..}}，
#          响应取 output.embeddings[*].embedding。
#   2. Mock 回退：**未配置真实 API Key 时返回确定性伪向量**，
#      使「解析 → 切片 → 写向量」全链路在本地可跑通（向量不具语义相似度，仅验证管线）。
#      拿到真实 Key 后填入 .env 的 QWEN_API_KEY 即自动切换，无需改代码。
#   3. 分批 + 重试：按 EMBEDDING_BATCH_SIZE 分批，
#      失败按 EMBEDDING_MAX_RETRIES 线性退避重试，避免偶发抖动导致整篇文档入库失败。

from __future__ import annotations

import asyncio
import hashlib
import math
import random
from typing import List

import httpx

from ..config import get_settings
from ..core import egress
from ..core.logging import get_logger

logger = get_logger(__name__)


class EmbeddingError(Exception):
    """向量化失败（接口报错 / 网络不可达 / 响应格式异常）。

    上层（services/indexing_service.py）捕获后把原因写入
    documents.last_index_error，并把文档状态回退为可重试状态。
    """


def mock_embedding(text: str, dim: int) -> List[float]:
    """生成**确定性**伪向量（无真实语义，仅供管线联调）。

    相同文本恒定得到相同向量（便于复现与断言），不同文本得到不同向量，
    但向量间的相似度不反映语义相关性，因此**不可用于真实检索评估**。

    Args:
        text: 输入文本。
        dim: 向量维度。

    Returns:
        长度 dim 的 L2 归一化向量（各分量落在 [-1, 1]）。
    """
    # 以文本哈希作为随机种子：保证「同文本 → 同向量」
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    vector = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    # L2 归一化：与真实 embedding 的常见约定一致，便于后续余弦相似度计算
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _build_request_payload(texts: List[str], model: str, dim: int) -> dict:
    """构造 DashScope 多模态向量接口的请求体。

    Args:
        texts: 本批待向量化的文本列表。
        model: 模型名（qwen3-vl-embedding）。
        dim: 期望向量维度（通过 parameters.dimension 指定）。

    Returns:
        可直接 JSON 序列化的请求体字典。
    """
    return {
        "model": model,
        # 多模态接口用 contents 数组承载输入；文本场景每个元素只含 text 字段
        # （P5 扩展图片时，元素形如 {"image": "data:image/png;base64,..."}）
        "input": {"contents": [{"text": t} for t in texts]},
        "parameters": {"dimension": dim},
    }


def _parse_response(data: dict, expected: int) -> List[List[float]]:
    """解析接口响应，取出向量列表并做基本校验。

    Args:
        data: 接口返回的 JSON 字典。
        expected: 期望的向量条数（用于校验响应完整）。

    Returns:
        向量列表（每项长度等于配置维度）。

    Raises:
        EmbeddingError: 响应结构异常或条数不匹配。
    """
    try:
        embeddings = data["output"]["embeddings"]
        # 接口不保证按输入顺序返回，故按 index 排序后还原顺序
        embeddings = sorted(embeddings, key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in embeddings]
    except (KeyError, TypeError) as exc:
        raise EmbeddingError(f"向量接口响应格式异常：{exc}；原始响应：{str(data)[:200]}") from exc

    if len(vectors) != expected:
        raise EmbeddingError(
            f"向量接口返回条数不匹配：期望 {expected} 条，实际 {len(vectors)} 条"
        )
    return vectors


def _build_openai_payload(texts: List[str], model: str, dim: int) -> dict:
    """构造 OpenAI 兼容 embeddings 接口的请求体。

    阿里云百炼 compatible-mode 网关、本地 vLLM 等 OpenAI 兼容服务通用：
    以字符串数组作为 input，并用 dimensions 锁定输出维度（确保与 pgvector 列一致）。

    Args:
        texts: 本批待向量化的文本列表。
        model: 模型名（如 text-embedding-v4）。
        dim: 期望向量维度（>0 时携带 dimensions 参数）。

    Returns:
        可直接 JSON 序列化的请求体字典。
    """
    payload: dict = {"model": model, "input": list(texts)}
    # dimensions 锁定输出维度，避免模型默认维度与 pgvector 列 vector(DIM) 不一致导致写入失败；
    # 目标模型 text-embedding-v4 支持该参数，部分模型若不支持可置 0 关闭（本项目固定 1024）。
    if dim and dim > 0:
        payload["dimensions"] = dim
    return payload


def _parse_openai_response(data: dict, expected: int) -> List[List[float]]:
    """解析 OpenAI 兼容 embeddings 响应，取出向量列表并做基本校验。

    Args:
        data: 接口返回的 JSON 字典（形如 {"data":[{"index":0,"embedding":[..]}]}）。
        expected: 期望的向量条数。

    Returns:
        向量列表（每项长度等于配置维度）。

    Raises:
        EmbeddingError: 响应结构异常或条数不匹配。
    """
    try:
        items = data["data"]
        # 接口不保证按输入顺序返回，故按 index 排序后还原顺序
        items = sorted(items, key=lambda it: it.get("index", 0))
        vectors = [it["embedding"] for it in items]
    except (KeyError, TypeError) as exc:
        raise EmbeddingError(f"向量接口响应格式异常：{exc}；原始响应：{str(data)[:200]}") from exc

    if len(vectors) != expected:
        raise EmbeddingError(
            f"向量接口返回条数不匹配：期望 {expected} 条，实际 {len(vectors)} 条"
        )
    return vectors


def _resolve_api_mode(settings) -> str:
    """根据配置解析当前使用的 embedding 协议模式。

    - 显式指定 openai / dashscope 时直接采用；
    - auto（默认）按 BASE_URL 推断：含 "compatible-mode" 或以 "/v1" 结尾 → openai，
      否则视为 DashScope 原生多模态向量接口。

    Args:
        settings: 全局配置实例。

    Returns:
        "openai" 或 "dashscope"。
    """
    mode = (settings.QWEN_EMBEDDING_API_MODE or "auto").strip().lower()
    if mode in ("openai", "openai-compatible", "compatible"):
        return "openai"
    if mode in ("dashscope", "multimodal", "qwen-vl"):
        return "dashscope"
    # auto：根据 BASE_URL 特征推断
    base = (settings.QWEN_EMBEDDING_BASE_URL or "").lower()
    if "compatible-mode" in base or base.rstrip("/").endswith("/v1"):
        return "openai"
    return "dashscope"


async def _call_qwen_batch(texts: List[str]) -> List[List[float]]:
    """调用真实 Embedding 接口向量化**一批**文本（含重试）。

    根据 _resolve_api_mode 自动选择协议：
    - openai 模式：POST {BASE_URL}/embeddings，OpenAI 兼容请求/响应格式；
    - dashscope 模式：POST {BASE_URL}（已是完整 endpoint），DashScope 多模态请求/响应格式。

    Args:
        texts: 本批文本（条数不超过 EMBEDDING_BATCH_SIZE）。

    Returns:
        本批文本对应的向量列表。

    Raises:
        EmbeddingError: 重试后仍失败（网络/鉴权/配额/响应异常）。
    """
    settings = get_settings()
    mode = _resolve_api_mode(settings)
    headers = {
        "Authorization": f"Bearer {settings.QWEN_API_KEY}",
        "Content-Type": "application/json",
    }

    if mode == "openai":
        # OpenAI 兼容 embeddings 接口：BASE_URL 通常是 ".../v1"，补 /embeddings 成完整路径
        url = settings.QWEN_EMBEDDING_BASE_URL.rstrip("/") + "/embeddings"
        payload = _build_openai_payload(
            texts, settings.QWEN_EMBEDDING_MODEL, settings.EMBEDDING_DIM
        )
        parse = lambda data: _parse_openai_response(data, len(texts))
    else:
        # DashScope 原生多模态向量接口：BASE_URL 已是完整 endpoint
        url = settings.QWEN_EMBEDDING_BASE_URL
        payload = _build_request_payload(
            texts, settings.QWEN_EMBEDDING_MODEL, settings.EMBEDDING_DIM
        )
        parse = lambda data: _parse_response(data, len(texts))

    last_error: Exception | None = None
    # P7-B2（数据不出域）：向量接口会把**文档正文**送出本域，必须先过合规校验。
    # 校验放在循环外：被拦截属于配置问题，重试没有意义。
    egress.check_outbound(url, "embedding")
    # 简单线性退避重试：第 n 次失败后等待 0.5*(n+1) 秒
    for attempt in range(settings.EMBEDDING_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.EMBEDDING_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                # 4xx 多为鉴权/参数问题，重试无意义，直接抛出并带上响应体摘要
                raise EmbeddingError(
                    f"向量接口返回 {resp.status_code}：{resp.text[:200]}"
                )
            return parse(resp.json())
        except EmbeddingError as exc:
            last_error = exc
            # 4xx（鉴权/参数）不再重试，5xx/网络类才继续
            if "返回 4" in str(exc):
                break
        except (httpx.HTTPError, ValueError) as exc:
            # 网络异常 / JSON 解析失败：可重试
            last_error = exc

        if attempt < settings.EMBEDDING_MAX_RETRIES:
            await asyncio.sleep(0.5 * (attempt + 1))

    raise EmbeddingError(f"调用向量接口失败（已重试 {settings.EMBEDDING_MAX_RETRIES} 次）：{last_error}")


async def embed_texts(texts: List[str]) -> List[List[float]]:
    """批量把文本转换为向量（入库与检索的统一入口）。

    - 未配置真实 API Key：返回 mock 向量（本地跑通管线用）；
    - 已配置：按 EMBEDDING_BATCH_SIZE 分批调用真实接口，串行执行以避免触发限流。

    Args:
        texts: 待向量化的文本列表。

    Returns:
        与 texts 一一对应的向量列表。

    Raises:
        EmbeddingError: 真实调用失败（mock 模式不会抛错）。
        ValueError: texts 为空（调用方应先判空，避免无意义请求）。
    """
    if not texts:
        raise ValueError("待向量化的文本列表为空")

    settings = get_settings()
    dim = settings.EMBEDDING_DIM

    # Mock 模式：Key 未配置（本地开发/联调），只跑通管线
    if not settings.is_qwen_configured:
        logger.warning(
            "QWEN_API_KEY 未配置，使用 mock 向量（%d 条）——向量无语义，仅供管线联调",
            len(texts),
        )
        return [mock_embedding(text, dim) for text in texts]

    # 真实调用：分批串行处理（避免触发限流）
    mode = _resolve_api_mode(settings)
    batch_size = max(1, settings.EMBEDDING_BATCH_SIZE)
    vectors: List[List[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(await _call_qwen_batch(batch))
        logger.info(
            "向量化进度：%d/%d（协议=%s，模型=%s）",
            min(start + batch_size, len(texts)),
            len(texts),
            mode,
            settings.QWEN_EMBEDDING_MODEL,
        )

    # 维度兜底校验：把「模型返回维度与配置不一致」提前暴露，避免写入 PG 时才报维度错误
    for vec in vectors:
        if len(vec) != dim:
            raise EmbeddingError(
                f"向量维度不匹配：配置 {dim} 维，实际返回 {len(vec)} 维"
            )
    return vectors


async def embed_query(query: str) -> List[float]:
    """把检索查询语句转换为向量（P4 检索召回使用，语义同 embed_texts）。

    Args:
        query: 用户提问 / 检索语句。

    Returns:
        单个向量。
    """
    vectors = await embed_texts([query])
    return vectors[0]


# =====================================================================
# 向量模型签名与「向量已过期」判定（P4 功能1 遗留问题修复）
# ---------------------------------------------------------------------
# 为什么需要签名：
#   向量只有在「同一模型、同一语义空间」内才能比较。以下两种情况会让库里
#   的向量与当前查询向量不可比，检索结果等同于随机：
#     1. 入库时未配置真实 Key → 写入的是 mock 伪向量；
#     2. 入库后又更换了 embedding 模型。
#   把「入库时用的模型」记在 documents.embedding_model 上，接口层比对即可提示重入库。
# =====================================================================

# mock 模式的签名标记：与真实模型名区分开，便于后台一眼看出「这是伪向量」
MOCK_SIGNATURE = "mock"


def current_embedding_signature() -> str:
    """返回**当前配置**下会使用的向量模型签名。

    Returns:
        已配置真实 Key 时返回模型名（如 qwen3.7-text-embedding-flash）；
        未配置（走 mock）时返回 "mock"。
    """
    settings = get_settings()
    return settings.QWEN_EMBEDDING_MODEL if settings.is_qwen_configured else MOCK_SIGNATURE


def embedding_is_stale(stored_signature: str | None) -> bool:
    """判断某文档已入库的向量是否与当前配置「不可比」。

    判定规则：
      - 记录为空（历史数据未记录模型）→ 视为需要重新入库：
        无法确认它用的是真实模型还是 mock，提示一次重入库即可自愈（代价很小）；
      - 与当前签名一致 → 不过期；
      - 不一致（mock ↔ 真实模型、或换了模型）→ 过期。

    Args:
        stored_signature: documents.embedding_model 的值。

    Returns:
        True 表示建议重新入库。
    """
    if not stored_signature:
        return True
    return stored_signature != current_embedding_signature()
