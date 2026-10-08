# DeepSeek 大模型流式调用客户端（P1-B1）
# 职责：以 OpenAI 兼容协议调用 DeepSeek 的 /chat/completions 流式接口，
#       把上游返回的 SSE 增量（delta）解析为逐段的文本片段，供 /chat 转发给前端。
#
# 说明：本模块是「最小可用」的直连实现（httpx）。
#      P1-A1 会用 LangChain 封装同样的 DeepSeek-V4.1-Flash 调用链并接管本模块职责，
#      因此这里刻意保持无框架依赖、易替换：对外只暴露 stream_chat() 一个生成器函数。

import json
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from ..config import Settings, get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)

# 上游流式响应的结束哨兵（OpenAI 兼容协议固定值）
_UPSTREAM_DONE = "[DONE]"


class DeepSeekError(Exception):
    """DeepSeek 调用失败。

    携带 status_code 便于上层区分「鉴权失败 / 限流 / 服务异常」并给出不同提示。
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def build_chat_messages(
    system_prompt: str,
    history: Optional[List[Dict[str, str]]],
    user_message: str,
) -> List[Dict[str, str]]:
    """组装发送给 DeepSeek 的消息数组。

    顺序固定为：system（人设/约束）→ 历史多轮 → 当前提问。
    历史已在调用方按窗口截断，避免 token 溢出（P2 会做更完整的窗口管理）。

    Args:
        system_prompt: 系统提示词。
        history: 历史消息列表，元素形如 {"role": "user", "content": "..."}。
        user_message: 用户本轮提问。

    Returns:
        DeepSeek 协议要求的消息数组。
    """
    messages: List[Dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for item in history or []:
        # 只透传合法角色的消息，避免脏数据导致上游 400
        if item.get("role") in ("user", "assistant", "system") and item.get("content"):
            messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": user_message})
    return messages


async def stream_chat(
    messages: List[Dict[str, str]],
    settings: Optional[Settings] = None,
    meta: Optional[dict] = None,
) -> AsyncIterator[str]:
    """调用 DeepSeek 流式生成，逐段产出回答文本。

    用法：
        async for chunk in stream_chat(messages):
            yield sse_content(chunk)

    Args:
        messages: 已组装好的消息数组（system + 历史 + 当前提问）。
        settings: 配置对象；不传则使用全局单例。
        meta: 可选的可变字典，用于回传上游元信息（当前写入 finish_reason），
              便于调用方判断回答是否因 max_tokens 上限被截断。

    Yields:
        模型增量输出的文本片段（可能为空串的上游心跳不会被产出）。

    Raises:
        DeepSeekError: 缺少 API Key 或上游返回非 2xx、连接异常。
    """
    cfg = settings or get_settings()

    # 前置校验：缺少可用 Key 时快速失败，避免发起必然 401 的请求
    if not cfg.is_deepseek_configured:
        raise DeepSeekError(
            "DeepSeek API Key 未配置，请在 backend/.env 中填写 DEEPSEEK_API_KEY",
            status_code=None,
        )

    # OpenAI 兼容协议的聊天补全地址（流式）
    url = f"{cfg.DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload: Dict[str, Any] = {
        "model": cfg.DEEPSEEK_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": cfg.DEEPSEEK_TEMPERATURE,
        "max_tokens": cfg.DEEPSEEK_MAX_TOKENS,
    }

    # 连接超时与读取超时分开设置：读取超时需大于模型首字延迟
    timeout = httpx.Timeout(10.0, read=cfg.DEEPSEEK_TIMEOUT)

    logger.info(
        "调用 DeepSeek：model=%s，消息数=%d，max_tokens=%d",
        cfg.DEEPSEEK_MODEL,
        len(messages),
        cfg.DEEPSEEK_MAX_TOKENS,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                # 非 2xx：读取错误体后抛出，便于定位（鉴权/限流/参数错误）
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="ignore")
                    raise DeepSeekError(
                        f"DeepSeek 返回 {response.status_code}：{body[:500]}",
                        status_code=response.status_code,
                    )

                # 逐行解析上游 SSE：data: {...} / data: [DONE]
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == _UPSTREAM_DONE:
                        break

                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        # 非 JSON（理论上不应出现）：忽略该帧，避免污染回答内容
                        logger.warning("无法解析的 DeepSeek 数据帧：%r", data[:200])
                        continue

                    # 捕获结束原因：若为 "length" 表示触达 max_tokens 上限被截断，
                    # 通过 meta 回传给调用方，用于向前端明确给出「已截断」提示。
                    choices = payload.get("choices") or []
                    if choices:
                        finish_reason = choices[0].get("finish_reason")
                        if finish_reason and meta is not None:
                            meta["finish_reason"] = finish_reason

                    chunk = _extract_delta(data)
                    if chunk:
                        yield chunk

    except httpx.HTTPError as exc:
        # 网络层异常（连接失败、超时等）统一包装为业务异常
        raise DeepSeekError(f"DeepSeek 请求失败：{exc}") from exc


def _extract_delta(raw: str) -> str:
    """从上游 SSE 的 data 载荷中提取本段增量文本。

    Args:
        raw: `data:` 后的 JSON 字符串。

    Returns:
        本段增量文本；无内容（如首帧角色帧、usage 帧）时返回空串。
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # 非 JSON（理论上不应出现）：忽略该帧，避免污染回答内容
        logger.warning("无法解析的 DeepSeek 数据帧：%r", raw[:200])
        return ""

    choices = payload.get("choices") or []
    if not choices:
        # 部分实现会在最后一个 data 帧返回 usage 统计，可在此记录用量
        usage = payload.get("usage")
        if usage:
            logger.info("DeepSeek token 用量：%s", usage)
        return ""

    delta = choices[0].get("delta") or {}
    # 标准 OpenAI 协议：正文在 delta.content；
    # 推理型模型另有 reasoning_content（思考过程），P1 不下发给前端。
    content = delta.get("content")
    return content if isinstance(content, str) else ""
