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
#
# P4 增强（本次迭代：RAG 增强回答可见）：
#   - 检索增强：回答前先做知识库检索（pgvector 相似度召回 → 重排 → 上下文拼装），
#     把命中的资料注入 Prompt，使回答「有据可依」（见 services/retriever.py）。
#   - 检索可见：在会话回绑帧之后、正文分片之前，额外下发一帧
#         data: {"type":"retrieval","mode":"rag|no_hit|disabled|fallback","count":N,...}
#     前端据此在助手气泡下方渲染「知识库检索 · 命中 N 段资料」等状态。
#   - 降级优先：检索是增强项，任何失败（DB/向量接口/无库）都只影响 mode，
#     绝不中断对话——无检索时退化为 P1/P2 的通用回答。
#
# P4 功能2 增强（答案引用来源）：
#   - 引用来源：回答生成后解析正文中的 [n] 标注，映射回检索到的切片，
#     在正文之后、[DONE] 之前追加一帧：
#         data: {"type":"citations","items":[{index,doc_id,doc_title,chunk_id,snippet,...}]}
#   - 引用持久化：同批引用写入 messages.citations（JSONB），
#     历史回看（GET /conversations/{id}）原样回传，前端复用同一套卡片渲染。
#
# P6 功能2 增强（答案反馈与评估）：
#   - 消息回执：本轮对话落库后、[DONE] 之前追加一帧
#         data: {"type":"message","id":"<助手消息ID>","user_message_id":"<用户消息ID>"}
#     前端据此把「本地临时气泡 ID」换成数据库主键，点赞/点踩才有目标
#     （见 POST /messages/{id}/feedback）。

import asyncio
import time
from typing import AsyncIterator, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from starlette.responses import Response

from ..config import get_settings
from ..core import metrics, rate_limit
from ..core.errors import DeepSeekError, RateLimitExceeded
from ..core.logging import get_logger
from ..core.security import get_current_user_id, get_current_user_id_sse
from ..core.sse import sse_comment, sse_content, sse_done, sse_headers, sse_json
from ..db.engine import SessionLocal
from ..schemas import ChatMessage, ChatRequest, QuotaResponse
from ..services import conversation_service as svc
from ..services.chat_chain import stream_answer
from ..services.citation_guard import guard_answer
from ..services.retriever import retrieve_for_chat, select_citations

logger = get_logger(__name__)

# 路由集合；main.py 中统一注册到应用
router = APIRouter(tags=["chat"])

# GET 分支的提问长度上限：与 POST 保持一致，防止超长 query 打爆请求行
_MAX_MESSAGE_LENGTH = 8000


async def _guard_llm_quota(
    user_id: UUID = Depends(get_current_user_id_sse),
) -> rate_limit.QuotaStatus:
    """LLM 配额守门依赖（P7-B1）：进入流式生成前校验并消耗一次额度。

    为什么做成依赖而不是在生成器里判断：
        SSE 一旦开始下发数据就无法再改状态码（HTTP 头已发出），
        必须在**返回 StreamingResponse 之前**拦截，才能给出规范的 429。

    Raises:
        RateLimitExceeded: 超出分钟/日次数或 token 配额（由全局处理器转 429）。
    """
    decision = await rate_limit.check_and_consume(user_id)
    if not decision.allowed:
        metrics.inc_llm_request("rate_limited")
        raise RateLimitExceeded(
            message=decision.message,
            scope=decision.scope,
            limit=decision.limit,
            retry_after=decision.retry_after,
            window="每分钟" if decision.scope == "minute" else "今日",
        )
    return decision


@router.get(
    "/chat/quota",
    response_model=QuotaResponse,
    summary="查询对话配额",
    description=(
        "返回当前用户的限流配置与剩余额度（只读，不消耗额度）。"
        "前端用它替代「等 429」——EventSource 拿不到 429 的响应体，"
        "先查配额才能给出可读提示。"
    ),
)
async def get_chat_quota(
    user_id: UUID = Depends(get_current_user_id),
) -> QuotaResponse:
    """查询当前用户的对话配额余量。"""
    decision = await rate_limit.get_quota_status(user_id)
    return QuotaResponse(
        enabled=decision.enabled,
        allowed=decision.allowed,
        scope=decision.scope,
        message=decision.message,
        retry_after=decision.retry_after,
        minute_used=decision.minute_used,
        minute_limit=decision.minute_limit,
        day_used=decision.day_used,
        day_limit=decision.day_limit,
        tokens_used=decision.tokens_used,
        tokens_quota=decision.tokens_quota,
    )


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
    # P7-B1 配额：超限直接 429（不会建立 SSE 连接）
    quota: rate_limit.QuotaStatus = Depends(_guard_llm_quota),
) -> Response:
    """SSE 流式对话（GET 入口，供 EventSource 消费）。"""
    return _stream_response(
        message=message,
        conversation_id=conversation_id,
        history=None,
        user_id=user_id,
        quota=quota,
    )


@router.post("/chat", response_class=StreamingResponse)
async def chat_post(
    payload: ChatRequest,
    # P2-B2 鉴权：POST 走 fetch，优先用 Authorization 头；依赖内部同样兼容 ?token=
    user_id: UUID = Depends(get_current_user_id_sse),
    # P7-B1 配额：超限返回 429 JSON（fetch 场景前端能读到 detail）
    quota: rate_limit.QuotaStatus = Depends(_guard_llm_quota),
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
        quota=quota,
    )


def _stream_response(
    message: str,
    conversation_id: Optional[str],
    history: Optional[List[ChatMessage]],
    user_id: UUID,
    quota: Optional[rate_limit.QuotaStatus] = None,
) -> StreamingResponse:
    """构造 SSE 流式响应。

    Args:
        message: 用户本轮提问。
        conversation_id: 会话 ID（可为空，落到默认会话或新建）。
        history: 客户端显式传入的历史；数据库可用时忽略，回退内存路径时使用。
        user_id: 当前登录用户 UUID（已由鉴权依赖解析，本函数不再做身份判断）。
        quota: 本轮已消耗的配额状态（用于回答结束后累加 token 用量）。

    Returns:
        StreamingResponse：media_type 为 text/event-stream，逐帧推送回答片段。
    """
    return StreamingResponse(
        _event_generator(
            message=message,
            conversation_id=conversation_id,
            history=history,
            user_id=user_id,
            quota=quota,
        ),
        media_type="text/event-stream",
        # 关闭 nginx 缓冲 + 禁用缓存，保证真正的「边生成边下发」
        headers=sse_headers(),
    )


async def _record_usage(
    user_id: UUID,
    message: str,
    answer: str,
    meta: dict,
    elapsed: float,
) -> None:
    """回答结束后记录 token 用量与调用指标（P7-B1）。

    优先使用上游返回的 usage（准确）；上游未返回时按字符数估算
    （估算偏保守，宁可高估让配额更早触发，也不要低估导致成本失控）。

    Args:
        user_id: 用户 ID（按用户累计配额）。
        message: 用户提问（估算输入 token 用）。
        answer: 完整回答（估算输出 token 用）。
        meta: 流式过程中收集的元信息（可能含 usage）。
        elapsed: 本轮耗时（秒）。
    """
    prompt_tokens = int(meta.get("prompt_tokens") or 0)
    completion_tokens = int(meta.get("completion_tokens") or 0)
    if prompt_tokens <= 0:
        prompt_tokens = rate_limit.estimate_tokens(message)
    if completion_tokens <= 0:
        completion_tokens = rate_limit.estimate_tokens(answer)

    try:
        await rate_limit.record_tokens(user_id, prompt_tokens, completion_tokens)
    except Exception as exc:  # noqa: BLE001 —— 记账失败不影响已完成的回答
        logger.warning("token 用量记录失败（已忽略）：%s", exc)

    metrics.inc_llm_request("success" if answer else "error")
    metrics.observe_llm_duration(elapsed)


async def _event_generator(
    message: str,
    conversation_id: Optional[str],
    history: Optional[List[ChatMessage]],
    user_id: UUID,
    quota: Optional[rate_limit.QuotaStatus] = None,
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
         - P4：执行知识库检索并下发 {"type":"retrieval",...} 帧，把资料注入 Prompt；
         - 流式生成；结束（且有完整回答）后：
             · 解析引用标注并下发 {"type":"citations",...} 帧（功能2）；
             · 落库本轮对话（含引用关系）；
      4. 若数据库不可用，回退到 P1 内存记忆路径（不持久化、不检索），保证开发期无 DB 也能对话；
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

            # P4-A1 / P4-B1：知识库检索（向量召回 → 重排 → 上下文）。
            # retrieve_for_chat 内部吞掉全部异常并给出 mode，保证检索失败也能正常对话；
            # 结果同时下发给前端，用于渲染「命中 N 段资料 / 未命中 / 未启用」状态。
            retrieval = await retrieve_for_chat(message, db)
            # P7-Dep1：检索模式分布进指标（Grafana 上可看「有多少提问真的命中了知识库」）
            metrics.inc_rag(retrieval.mode)
            yield sse_json(retrieval.meta_payload())

            # 流式生成：历史来自 DB，关闭内存记忆（memory_enabled=False），避免双写；
            # context 非 None 即走检索增强链（无命中时为空串，由 Prompt 引导模型如实说明）。
            answer_parts: List[str] = []
            # P7-B1：记录本轮耗时，用于 LLM 调用耗时直方图（P95/P99 是容量规划依据）
            llm_started = time.perf_counter()
            try:
                async for chunk in stream_answer(
                    message,
                    conversation_id=str(conv.id),
                    history=db_history,
                    meta=meta,
                    memory_enabled=False,
                    context=retrieval.context,
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

            # P7-B1：回答结束即结算用量（token 配额 + 调用指标），
            # 放在落库之前——即使落库失败，已经消耗的模型费用也必须计入配额。
            await _record_usage(
                user_id=user_id,
                message=message,
                answer=full_answer,
                meta=meta,
                elapsed=time.perf_counter() - llm_started,
            )

            # P4-B2：生成本轮引用来源（只保留回答实际标注过的资料；未标注时回退为全部检索资料）。
            # 必须放在「回答生成之后」——只有拿到完整回答，才能解析出 [n] 标注做来源映射。
            # 回答为空（生成失败 / 客户端中断）时不下发引用：
            # 否则会把「一段资料」附在一句「[回答失败]」下面，看起来像是该失败回答的来源。
            citations = select_citations(retrieval.chunks, full_answer) if full_answer else []

            # P4-B2 守卫：校验「正文出现过的每个编号 ⊆ citations 中的编号」这条不变量。
            # 不通过说明模型编了引用（零命中或越界时最容易发生），这里**只上报不改写**：
            # 流式 token 早已推给前端，事后剥离救不了当次页面，
            # 真正的修复在生成前分流（见 chat_chain 的三路分支）。指标告警见 metrics。
            if full_answer:
                guard_answer(full_answer, citations)

            if citations:
                # 引用帧：前端用它渲染「回答下方」的引用卡片（P4-F2）。
                # 放在 [DONE] 之前、正文之后，不打断流式渲染。
                yield sse_json({"type": "citations", "items": citations})

            if full_answer:
                try:
                    saved = await svc.append_turn(
                        db, conv.id, message, full_answer, citations=citations
                    )
                    # P6-B2：把落库后的真实消息 ID 回传前端。
                    # 前端流式气泡用的是本地临时 ID，无法用于 /messages/{id}/feedback；
                    # 只有拿到数据库主键，点赞/点踩按钮才有可指向的目标。
                    # 放在 [DONE] 之前下发，且失败不影响主流程（见下方 except）。
                    # 注：对返回值的类型做防御性判断——单测里 append_turn 常被替换为
                    # 返回 None 的桩函数，硬解包会抛 TypeError 并污染「落库成功」的语义。
                    if isinstance(saved, tuple) and len(saved) == 2:
                        yield sse_json(
                            {
                                "type": "message",
                                "id": str(saved[1]),          # 助手消息 ID（反馈目标）
                                "user_message_id": str(saved[0]),  # 用户消息 ID（便于排查）
                            }
                        )
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
    # P4：无数据库时无法查询向量表，这里仍下发一帧「检索未启用」，
    # 使前端对 retrieval 帧的处理逻辑保持单一（有帧即渲染状态，不区分链路）。
    fallback_retrieval = await retrieve_for_chat(message, None)
    metrics.inc_rag(fallback_retrieval.mode)
    yield sse_json(fallback_retrieval.meta_payload())
    llm_started = time.perf_counter()
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

    # P7-B1：无数据库路径同样结算用量（限流配额按用户计，与是否落库无关）
    await _record_usage(
        user_id=user_id,
        message=message,
        answer=full_answer,
        meta=meta,
        elapsed=time.perf_counter() - llm_started,
    )

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
