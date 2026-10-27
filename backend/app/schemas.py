# 接口的请求 / 响应数据模型（P1-B1）
# 职责：用 Pydantic 明确 /chat 等接口的入参约束，FastAPI 据此自动校验与生成 OpenAPI 文档。

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# 消息角色：与 DeepSeek（OpenAI 兼容协议）一致
Role = Literal["system", "user", "assistant"]


# =====================================================================
# 文档上传（P1-B2 / P1-F3 契约）
# 字段命名严格对齐前端 src/types.ts 的 DocumentRecord，便于前端直接反序列化。
# =====================================================================

class DocumentUploadResponse(BaseModel):
    """POST /documents/upload 的成功响应。

    字段与前端 DocumentRecord 一一对应：
      id / filename / type / status / size / storage_path / created_at
    其中后端库内字段 original_path → storage_path，size_bytes → size。
    """

    id: str = Field(..., description="文档唯一 ID（后端 UUID）")
    filename: str = Field(..., description="原始文件名（展示用）")
    # 文档类型：word（.doc/.docx）/ markdown（.md）
    type: str = Field(..., description="文档类型：word / markdown")
    # 生命周期状态：P1 上传成功固定为 pending（待审核）
    status: str = Field(..., description="文档状态：pending / indexed / rejected")
    # 文件字节数（对应库内 size_bytes）
    size: int = Field(..., description="文件大小（字节）")
    # 对象存储相对路径（不含 bucket），仅展示用，禁止前端拼接成下载链接
    storage_path: str = Field(..., description="对象存储相对路径（展示用）")
    # 提交时间（ISO 8601）
    created_at: str = Field(..., description="提交时间（ISO 8601 字符串）")


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
