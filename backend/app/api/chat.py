# /chat 流式对话接口（P1-B1 骨架 / P1-A1 接入 LangChain 链路 / P2 接入会话持久化）
# 职责：接收用户提问 → 交给 LangChain 调用链（含多轮历史）→ 以 SSE 逐片回传前端。
#
# 提供两个入口（对应《开发任务清单》P1-B1 的「GET/POST /chat」）：
#   - GET  /chat?message=...&conversation_id=...
#       供前端 EventSource 使用。EventSource 原生只支持 GET 且不能带请求体，
#       所以提问内容走 query 参数（见 frontend/src/services/chatStream.ts）。
#   - POST /chat  JSON: {"message": "...", "conversation_id": "...", "history": [...]}
#       为长文本提问与后续阶段（P2 会话历史）预留；前端后续改用 fetch + ReadableStream 消费时切换。
#
# 两者共用同一套流式逻辑，返回均为 text/event-stream：
#   data: {"content":"片段"}\n\n  × N   →   data: [DONE]\n\n
#
# P2 增强（本次迭代）：
#   - 会话持久化：conversation_id 存在时按 user_id 隔离读取/写入 conversations / messages 表；
#     不存在时自动新建会话；首条消息自动回填标题。
#   - 历史注入：注入前从数据库加载「上下文窗口」内的历史（见 conversation_service.load_history_window），
#     由数据库作为历史唯一来源（memory_enabled=False，避免与内存记忆双写）。
#   - 会话回绑：流起始下发一帧 data: {"type":"conversation","id":"..."}，便于前端在「首次发送自动建会话」
#     时拿到会话 ID 并绑定到左侧列表。
#   - 容错：若数据库不可用（如本地开发未起 PG），自动回退到 P1 内存记忆路径，保证「无 DB 也能对话」。

import asyncio
from typing import AsyncIterator, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from starlette.responses import Response

from ..config import get_settings
from ..core.errors import DeepSeekError
from ..core.logging import get_logger
from ..core.security import get_current_user_id_sse
from ..core.sse import sse_comment, sse_content, sse_done, sse_headers, sse_json
from ..db.engine import SessionLocal
from ..schemas import ChatMessage, ChatRequest
from ..services import conversation_service as svc
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
    # P2-B2 鉴权：由依赖统一解析当前用户（令牌缺失/失效直接 401，不会进入流）。
    # EventSource 无法自定义请求头，故该依赖内部额外支持 ?token=xxx 传参。
    user_id: UUID = Depends(get_current_user_id_sse),
) -> Response:
    """SSE 流式对话（GET 入口，供 EventSource 消费）。"""
    return _stream_response(
        message=message, conversation_id=conversation_id, history=None, user_id=user_id
    )


@router.post("/chat", response_class=StreamingResponse)
async def chat_post(
    payload: ChatRequest,
    # P2-B2 鉴权：POST 走 fetch，优先用 Authorization 头；依赖内部同样兼容 ?token=
    user_id: UUID = Depends(get_current_user_id_sse),
) -> Response:
    """SSE 流式对话（POST 入口，支持长文本与显式历史）。

    说明（P2）：当 conversation_id 有效且数据库可用时，以数据库历史为唯一来源，
    忽略 payload.history（避免客户端历史与数据库历史重复叠加）；仅当数据库不可用回退
    内存记忆时，才会使用 payload.history。
    """
    return _stream_response(
        message=payload.message,
        conversation_id=payload.conversation_id,
        history=payload.history,
        user_id=user_id,
    )


def _stream_response(
    message: str,
    conversation_id: Optional[str],
    history: Optional[List[ChatMessage]],
    user_id: UUID,
) -> StreamingResponse:
    """构造 SSE 流式响应。

    Args:
        message: 用户本轮提问。
        conversation_id: 会话 ID（可为空，落到默认会话或新建）。
        history: 客户端显式传入的历史；数据库可用时忽略，回退内存路径时使用。
        user_id: 当前登录用户 UUID（已由鉴权依赖解析，本函数不再做身份判断）。

    Returns:
        StreamingResponse：media_type 为 text/event-stream，逐帧推送回答片段。
    """
    return StreamingResponse(
        _event_generator(
            message=message, conversation_id=conversation_id, history=history, user_id=user_id
        ),
        media_type="text/event-stream",
        # 关闭 nginx 缓冲 + 禁用缓存，保证真正的「边生成边下发」
        headers=sse_headers(),
    )


async def _event_generator(
    message: str,
    conversation_id: Optional[str],
    history: Optional[List[ChatMessage]],
    user_id: UUID,
) -> AsyncIterator[str]:
    """SSE 事件生成器：流式产出回答片段，并按 P2 约定做会话持久化与历史注入。

    流程：
      1. 心跳帧：立刻冲刷响应头；
      2. 当前用户 user_id 由路由的鉴权依赖解析后传入（P2-B2：JWT 校验，
         令牌无效时依赖直接返回 401，不会进入本生成器）；
      3. 尝试数据库持久化路径：
         - 新建/读取会话（user_id 隔离、归属校验）；
         - 下发 {"type":"conversation","id":...} 回绑帧；
         - 从数据库加载上下文窗口内历史，注入 LangChain（memory_enabled=False）；
         - 流式生成；结束（且有完整回答）后落库本轮对话；
      4. 若数据库不可用，回退到 P1 内存记忆路径（不持久化），保证开发期无 DB 也能对话；
      5. 统一的截断提示与结束哨兵。
    """
    # 1) 心跳帧：让前端尽快收到首字节（配合 X-Accel-Buffering: no 生效）
    yield sse_comment("connected")

    # 2) 当前用户已由鉴权依赖（get_current_user_id_sse）解析并传入，
    #    这里不再做身份判断——身份不可信的请求在进入生成器前就已被 401 拦截。

    # 把客户端传入的 conversation_id 解析为 UUID；非法/缺失都视为「需要新建/默认」
    conv_uuid: Optional[UUID] = None
    if conversation_id:
        try:
            conv_uuid = UUID(conversation_id)
        except (ValueError, AttributeError):
            conv_uuid = None

    cfg = get_settings()

    # 3) 尝试数据库持久化路径（P2 主路径）
    meta: dict = {}
    try:
        async with SessionLocal() as db:
            # 新建或读取会话（user_id 隔离 + 归属校验）
            conv = await svc.get_or_create_conversation(db, user_id, conv_uuid)
            # 回绑帧：前端在「首次发送自动建会话」时据此拿到会话 ID
            yield sse_json({"type": "conversation", "id": str(conv.id)})

            # 从数据库加载上下文窗口内的历史（P2-DB1：防 token 溢出）
            db_history = await svc.load_history_window(
                db, conv.id, cfg.MEMORY_MAX_TURNS, cfg.CONTEXT_TOKEN_BUDGET
            )

            # 流式生成：历史来自 DB，关闭内存记忆（memory_enabled=False），避免双写
            answer_parts: List[str] = []
            try:
                async for chunk in stream_answer(
                    message,
                    conversation_id=str(conv.id),
                    history=db_history,
                    meta=meta,
                    memory_enabled=False,
                ):
                    answer_parts.append(chunk)
                    yield sse_content(chunk)
            except DeepSeekError as exc:
                # 上游异常：把可读错误下发给前端展示（前端会渲染在助手气泡里）
                logger.error("DeepSeek 调用失败：%s", exc)
                yield sse_content(f"\n\n[回答失败] {exc}")
            except asyncio.CancelledError:
                # 客户端中断：记录后继续向上抛出，由 Starlette 关闭连接；不落库半截回答
                logger.info("流式回答被客户端中断：conversation_id=%s", conv.id)
                raise
            except Exception as exc:  # noqa: BLE001 ——兜底，保证流一定能正常收尾
                logger.exception("流式回答出现未预期错误")
                yield sse_content(f"\n\n[回答失败] 服务内部错误：{exc}")

            # 正常收尾：有完整回答才落库（避免把空回答/报错写入历史）
            full_answer = "".join(answer_parts)
            if full_answer:
                try:
                    await svc.append_turn(db, conv.id, message, full_answer)
                except Exception as exc:  # noqa: BLE001 ——落库失败仅记录，不影响已下发的流
                    logger.error("落库本轮对话失败：%s", exc)

            # 若因达到 max_tokens 上限被截断，明确提示用户
            if meta.get("finish_reason") == "length":
                logger.warning(
                    "回答因达到 max_tokens 上限被截断：conversation_id=%s，已生成 %d 字符",
                    conv.id,
                    len(full_answer),
                )
                yield sse_content(
                    "\n\n⚠️ 回答因达到长度上限（DEEPSEEK_MAX_TOKENS）被截断，后续内容已省略。"
                    "可在 backend/.env 调大 DEEPSEEK_MAX_TOKENS 后重试。"
                )

            # 结束哨兵：前端收到后关闭 EventSource
            yield sse_done()
            return

    except Exception as db_exc:  # noqa: BLE001 ——DB 不可用：回退内存记忆，保证开发期可用
        logger.warning("会话持久化路径不可用，回退到内存记忆（不落库）：%s", db_exc)

    # 4) 回退：P1 内存记忆路径（不持久化）——与 P1 行为一致，便于无 DB 环境联调
    meta = {}
    answer_parts = []
    try:
        async for chunk in stream_answer(
            message,
            conversation_id=conversation_id,
            history=[{"role": m.role, "content": m.content} for m in history] if history else None,
            meta=meta,
        ):
            answer_parts.append(chunk)
            yield sse_content(chunk)
    except DeepSeekError as exc:
        logger.error("DeepSeek 调用失败：%s", exc)
        yield sse_content(f"\n\n[回答失败] {exc}")
    except asyncio.CancelledError:
        logger.info("流式回答被客户端中断：conversation_id=%s", conversation_id)
        raise
    except Exception as exc:  # noqa: BLE001 ——兜底，保证流一定能正常收尾
        logger.exception("流式回答出现未预期错误")
        yield sse_content(f"\n\n[回答失败] 服务内部错误：{exc}")

    full_answer = "".join(answer_parts)
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
