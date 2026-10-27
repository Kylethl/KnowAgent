# /chat 流式对话接口（P1-B1 骨架 / P1-A1 接入 LangChain 链路）
# 职责：接收用户提问 → 交给 LangChain 调用链（含多轮记忆）→ 以 SSE 逐片回传前端。
#
# 提供两个入口（对应《开发任务清单》P1-B1 的「GET/POST /chat」）：
#   - GET  /chat?message=...&conversation_id=...
#       供前端 EventSource 使用。EventSource 原生只支持 GET 且不能带请求体，
#       所以提问内容走 query 参数（见 frontend/src/services/chatStream.ts）。
#   - POST /chat  JSON: {"message": "...", "conversation_id": "...", "history": [...]}
#       为长文本提问与 P2 会话历史预留；前端后续改用 fetch + ReadableStream 消费时切换。
#
# 两者共用同一套流式逻辑，返回均为 text/event-stream：
#   data: {"content":"片段"}\n\n  × N   →   data: [DONE]\n\n

import asyncio
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from starlette.responses import Response

from ..core.errors import DeepSeekError
from ..core.logging import get_logger
from ..core.sse import sse_comment, sse_content, sse_done, sse_headers
from ..schemas import ChatMessage, ChatRequest
from ..services.chat_chain import stream_answer

logger = get_logger(__name__)

# 路由集合；main.py 中统一注册到应用
router = APIRouter(tags=["chat"])

# GET 分支的提问长度上限：与 POST 保持一致，防止超长 query 打爆请求行
_MAX_MESSAGE_LENGTH = 8000


@router.get("/chat", response_class=StreamingResponse)
async def chat_get(
    message: str = Query(
        ...,
        min_length=1,
        max_length=_MAX_MESSAGE_LENGTH,
        description="用户提问内容（EventSource 仅支持 GET，故走 query）",
    ),
    conversation_id: Optional[str] = Query(
        default=None, description="会话 ID，用于多轮上下文；不传则使用默认会话"
    ),
) -> Response:
    """SSE 流式对话（GET 入口，供 EventSource 消费）。"""
    return _stream_response(message=message, conversation_id=conversation_id, history=None)


@router.post("/chat", response_class=StreamingResponse)
async def chat_post(payload: ChatRequest) -> Response:
    """SSE 流式对话（POST 入口，支持长文本与显式历史）。"""
    return _stream_response(
        message=payload.message,
        conversation_id=payload.conversation_id,
        history=payload.history,
    )


def _stream_response(
    message: str,
    conversation_id: Optional[str],
    history: Optional[List[ChatMessage]],
) -> StreamingResponse:
    """构造 SSE 流式响应。

    Args:
        message: 用户本轮提问。
        conversation_id: 会话 ID（可为空，落到默认会话）。
        history: 客户端显式传入的历史；为 None 时使用后端会话记忆。

    Returns:
        StreamingResponse：media_type 为 text/event-stream，逐帧推送回答片段。
    """
    return StreamingResponse(
        _event_generator(message=message, conversation_id=conversation_id, history=history),
        media_type="text/event-stream",
        # 关闭 nginx 缓冲 + 禁用缓存，保证真正的「边生成边下发」
        headers=sse_headers(),
    )


async def _event_generator(
    message: str,
    conversation_id: Optional[str],
    history: Optional[List[ChatMessage]],
) -> AsyncIterator[str]:
    """SSE 事件生成器：流式产出回答片段。

    流程：
      1. 先发一帧注释（心跳），立刻冲刷响应头，避免客户端/代理等待；
      2. 把请求交给 LangChain 调用链——多轮历史的读取/注入/写回由链路内部完成
         （见 services/chat_chain.py 与 services/langchain_memory.py）；
      3. 逐片转发模型增量；异常时把可读错误写进助手气泡，保证流能正常收尾。
    """
    # 1) 心跳帧：让前端尽快收到首字节（配合 X-Accel-Buffering: no 生效）
    yield sse_comment("connected")

    # 2) 客户端显式传入的历史优先（POST /chat）；GET 场景为 None，完全依赖服务端记忆
    client_history = [{"role": m.role, "content": m.content} for m in history] if history else None

    logger.info(
        "收到 /chat 请求：conversation_id=%s，客户端历史=%s",
        conversation_id or "default",
        len(client_history) if client_history else 0,
    )

    # 3) 逐片转发 LangChain 链路的输出
    # meta 用于回传上游 finish_reason，便于判断回答是否被 max_tokens 截断
    meta: dict = {}
    answer_parts: List[str] = []
    try:
        async for chunk in stream_answer(
            message,
            conversation_id=conversation_id,
            history=client_history,
            meta=meta,
        ):
            answer_parts.append(chunk)
            yield sse_content(chunk)
    except DeepSeekError as exc:
        # 上游异常：把可读错误下发给前端展示（前端会渲染在助手气泡里），
        # 而不是直接断开连接——否则前端只能显示「连接中断」。
        logger.error("DeepSeek 调用失败：%s", exc)
        yield sse_content(f"\n\n[回答失败] {exc}")
    except asyncio.CancelledError:
        # 客户端中断（点了「停止生成」或关闭页面）：记录后继续向上抛出，
        # 由 Starlette 负责关闭连接；此处不写入记忆，避免半截回答污染上下文。
        logger.info("流式回答被客户端中断：conversation_id=%s", conversation_id)
        raise
    except Exception as exc:  # noqa: BLE001 ——兜底，保证流一定能正常收尾
        logger.exception("流式回答出现未预期错误")
        yield sse_content(f"\n\n[回答失败] 服务内部错误：{exc}")

    # 4) 日志记录本轮规模（不再由此处写记忆：LangChain 链路在生成成功后已自行写回）
    full_answer = "".join(answer_parts)
    logger.info("流式回答结束：共 %d 个字符", len(full_answer))

    # 4.1) 若因达到 max_tokens 上限被截断，明确提示用户（否则无法区分「答完」与「被截断」）
    if meta.get("finish_reason") == "length":
        logger.warning(
            "回答因达到 max_tokens 上限被截断：conversation_id=%s，已生成 %d 字符",
            conversation_id or "default",
            len(full_answer),
        )
        yield sse_content(
            "\n\n⚠️ 回答因达到长度上限（DEEPSEEK_MAX_TOKENS）被截断，后续内容已省略。"
            "可在 backend/.env 调大 DEEPSEEK_MAX_TOKENS 后重试。"
        )

    # 结束哨兵：前端收到后关闭 EventSource
    yield sse_done()
