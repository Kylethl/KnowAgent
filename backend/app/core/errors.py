# 异常定义（P1-A1）
# 职责：把「大模型调用链路」可能出现的错误收敛为本项目统一的异常类型，
#       便于上层 API 区分「配置缺失 / 鉴权失败 / 限流 / 网络异常 / 服务异常」并给出针对性提示。
#
# 变更说明：DeepSeekError 原先定义在 services/deepseek_client.py（P1-B1 的 httpx 直连实现）。
# P1-A1 用 LangChain 重建调用链后，抽象层级提升为「通用 LLM 错误」，故上移到 core 层统一管理，
# 并保留 DeepSeekError 这个名称，使上层（api/chat.py）的异常捕获逻辑保持稳定。

from typing import Optional


class LLMError(Exception):
    """大模型调用异常的基类。

    上层只需捕获 LLMError 即可覆盖全部大模型侧失败，无需关心底层是
    LangChain 还是具体 SDK 抛出的异常类型。
    """


class DeepSeekError(LLMError):
    """DeepSeek 调用失败。

    Attributes:
        status_code: 上游 HTTP 状态码（如 401 鉴权失败、429 限流）。
            非 HTTP 类错误（网络超时、连接失败）为 None。
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class LLMNotConfiguredError(DeepSeekError):
    """未配置可用的 API Key。

    属于「应该由运维/开发者处理」的错误：不应发起必然失败的请求，
    而应尽早失败并在前端给出明确的「请配置 Key」提示。
    """

    def __init__(self, message: str = "大模型 API Key 未配置"):
        super().__init__(message, status_code=None)
