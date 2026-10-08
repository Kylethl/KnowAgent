# 接口的请求 / 响应数据模型（P1-B1）
# 职责：用 Pydantic 明确 /chat 等接口的入参约束，FastAPI 据此自动校验与生成 OpenAPI 文档。

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# 消息角色：与 DeepSeek（OpenAI 兼容协议）一致
Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    """一条对话消息（用于历史上下文注入）。"""

    role: Role = Field(..., description="消息角色：system / user / assistant")
    content: str = Field(..., description="消息正文")


class ChatRequest(BaseModel):
    """POST /chat 的请求体。

    P1 阶段前端使用 EventSource（仅支持 GET），因此实际走 GET 分支；
    POST 分支为长文本提问与后续阶段（P2 会话历史）预留。
    """

    # 用户本轮提问，必填且不能为空
    message: str = Field(..., min_length=1, max_length=8000, description="用户提问内容")
    # 会话 ID：不传则由后端按「单会话」处理（P2 起由前端传入真实会话）
    conversation_id: Optional[str] = Field(default=None, description="会话 ID，用于多轮上下文")
    # 可选：由客户端显式携带的历史消息（不传则用后端会话记忆）
    history: Optional[List[ChatMessage]] = Field(default=None, description="客户端携带的历史消息")


class HealthResponse(BaseModel):
    """健康检查响应（部署冒烟测试用）。"""

    status: str = Field(..., description="服务状态：ok / degraded")
    app: str = Field(..., description="应用名称")
    env: str = Field(..., description="运行环境")
    # 大模型是否配置了可用 Key，便于排查「能访问但答不了」的问题
    llm_ready: bool = Field(..., description="DeepSeek 是否已配置可用 API Key")
