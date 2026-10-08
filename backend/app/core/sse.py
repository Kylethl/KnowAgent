# SSE（Server-Sent Events）编码工具（P1-B1）
# 职责：把后端要下发的数据封装成符合 SSE 规范的文本帧，供 StreamingResponse 逐帧写出。
#
# SSE 帧格式约定（与前端 frontend/src/services/chatStream.ts 对齐）：
#   1) 内容帧：  data: {"content": "文字片段"}\n\n
#   2) 结束帧：  data: [DONE]\n\n            —— 前端收到即认为本轮回答结束并关闭连接
#   3) 注释帧：  : keep-alive\n\n            —— 心跳，占位防代理超时（可选）
#
# 采用 JSON 承载内容的原因：模型输出可能含换行，JSON 转义可保证单帧单行，
# 前端 extractContent() 同时兼容 JSON 与纯文本两种格式。

import json
from typing import Any, Dict, Optional

# 流结束哨兵，必须与前端 DONE_SIGNAL 常量保持一致
DONE_SIGNAL = "[DONE]"

# SSE 标准要求的帧分隔符：一帧以空行结束
_FRAME_SEPARATOR = "\n\n"


def sse_frame(data: str, event: Optional[str] = None) -> str:
    """将一段文本封装为 SSE 数据帧。

    Args:
        data: 帧内容（单行或含换行的文本）。含换行时每行都会被加上 `data: ` 前缀。
        event: 可选事件名；不传则为默认事件（前端 es.onmessage 可接收）。

    Returns:
        符合 SSE 规范的完整帧字符串（已带结尾空行）。
    """
    lines = []
    # 命名事件（如 event: error）——注意前端目前只监听默认事件，命名事件仅作扩展预留
    if event:
        lines.append(f"event: {event}")
    # 逐行加 data: 前缀；空内容也要输出一个空 data 行，保证帧合法
    for line in (data.splitlines() or [""]):
        lines.append(f"data: {line}")
    return "\n".join(lines) + _FRAME_SEPARATOR


def sse_json(payload: Dict[str, Any], event: Optional[str] = None) -> str:
    """将字典序列化为单行 JSON 的 SSE 帧。

    用于下发内容片段等结构化数据，避免换行破坏帧边界。
    """
    return sse_frame(json.dumps(payload, ensure_ascii=False), event=event)


def sse_content(content: str) -> str:
    """下发一个回答文本片段（前端据此做打字机渲染）。"""
    return sse_json({"content": content})


def sse_done() -> str:
    """下发流结束哨兵帧。"""
    return sse_frame(DONE_SIGNAL)


def sse_comment(comment: str = "keep-alive") -> str:
    """下发注释帧（心跳/占位），客户端会忽略，用于防止反向代理因空闲断开连接。"""
    return f": {comment}{_FRAME_SEPARATOR}"


def sse_headers() -> Dict[str, str]:
    """SSE 响应头。

    关键点：
    - Cache-Control/Connection：标准 SSE 要求，禁止缓存、保持长连接；
    - X-Accel-Buffering: no —— 关闭 nginx 响应缓冲，
      否则 P1-Dep1 的 nginx 反代会攒够一块才下发，前端看不到流式效果。
    """
    return {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
