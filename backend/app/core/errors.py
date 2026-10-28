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


class ConversationNotFound(Exception):
    """会话不存在、已被软删除、或不属于当前用户。

    统一用「找不到」语义，避免区分「不存在」与「不属于你」而泄露信息。
    上层（api/conversations.py）据此返回 404。
    """

    def __init__(self, conversation_id):
        super().__init__(f"会话不存在或无权访问：{conversation_id}")


# =====================================================================
# 用户鉴权异常（P2-B2）
# ---------------------------------------------------------------------
# 设计：把「认证」与「授权」两类失败分开，便于上层精确映射 HTTP 状态码：
#   认证失败（你是谁都没证明）→ 401；授权失败（你是谁知道了，但权限不够）→ 403。
# =====================================================================


class UnauthorizedError(Exception):
    """认证失败：令牌缺失 / 过期 / 签名不合法 / 用户已被禁用。

    上层（core/security.py 的鉴权依赖）据此返回 401，
    并在响应头带 WWW-Authenticate: Bearer，提示前端重新登录。
    """

    def __init__(self, message: str = "未认证或登录已失效，请重新登录"):
        super().__init__(message)
        self.message = message


class ForbiddenError(Exception):
    """授权失败：已认证但权限不足（P3 RBAC 的 admin 校验使用）。

    上层据此返回 403。
    """

    def __init__(self, message: str = "权限不足，无法执行该操作"):
        super().__init__(message)
        self.message = message


class UserAlreadyExists(Exception):
    """注册冲突：用户名或邮箱已被占用（对应 HTTP 409 Conflict）。

    Attributes:
        field: 冲突的字段名（username / email），便于前端高亮对应输入框。
    """

    def __init__(self, field: str = "username", message: Optional[str] = None):
        super().__init__(message or f"{'用户名' if field == 'username' else '邮箱'}已被占用")
        self.field = field


class InvalidCredentials(Exception):
    """登录失败：用户名不存在，或密码错误。

    安全约定：二者统一返回同一错误，不区分「用户不存在」与「密码错误」，
    避免被攻击者用来枚举已注册用户名。上层据此返回 401。
    """

    def __init__(self, message: str = "用户名或密码错误"):
        super().__init__(message)
        self.message = message


class WeakPasswordError(Exception):
    """密码强度不足（注册时校验）。

    上层据此返回 400，并把具体规则通过 message 回传给前端展示。
    """

    def __init__(self, message: str = "密码强度不足"):
        super().__init__(message)
        self.message = message


# =====================================================================
# 文档审核异常（P3-B1）
# ---------------------------------------------------------------------
# 设计：与会话一致，把「找不到文档」与「审核动作非法」分开：
#   前者 → 404（不区分「不存在」与「已软删除」，避免信息泄露）；
#   后者 → 409（状态机冲突，语义上是「当前状态不允许该操作」）。
# =====================================================================


class DocumentNotFound(Exception):
    """文档不存在或已被软删除。

    上层（api/admin.py 与后续 /documents 查询）据此返回 404。
    """

    def __init__(self, document_id=None):
        super().__init__(f"文档不存在或已被删除：{document_id}")
        self.message = str(self)


class ReviewNotAllowed(Exception):
    """当前文档状态不允许执行该审核动作（状态机冲突）。

    典型场景：
      - 文档已审核通过（review_status='approved'），不允许重复审核或驳回；
      - 驳回时未填写理由（需求明确要求「驳回必填理由」）。

    上层据此返回 409 Conflict。
    """

    def __init__(self, message: str = "当前状态不允许该审核操作"):
        super().__init__(message)
        self.message = message


# =====================================================================
# 文档入库异常（P3-B2）
# ---------------------------------------------------------------------
# 与审核异常保持同一风格：「找不到」→ 404，「状态不允许」→ 409，
# 「依赖服务失败」（对象存储 / 向量接口）→ 502。
# =====================================================================


class IndexingNotAllowed(Exception):
    """当前文档状态不允许入库（状态机冲突）。

    典型场景：文档尚未审核通过（review_status != 'approved'）就被要求入库。
    上层据此返回 409 Conflict。
    """

    def __init__(self, message: str = "当前状态不允许入库"):
        super().__init__(message)
        self.message = message


class IndexingInProgress(Exception):
    """文档正在入库中，重复触发被拒绝。

    说明：入库是耗时操作（解析 + 向量化），重复投递会造成切片重复与费用浪费。
    若上一次入库因进程重启等原因僵死（超过约定时长仍停留在 indexing），
    服务层会判定为「僵死」并允许重新开始，因此这里只在**确实正在进行**时抛出。
    上层据此返回 409 Conflict。
    """

    def __init__(self, message: str = "该文档正在入库中，请稍后再试"):
        super().__init__(message)
        self.message = message


# =====================================================================
# 运营闭环异常（P6：审核通知 + 答案反馈）
# ---------------------------------------------------------------------
# 沿用同一约定：找不到 → 404（不区分「不存在」与「不属于你」），
# 状态/入参不允许 → 400 或 409。
# =====================================================================


class NotificationNotFound(Exception):
    """通知不存在、已被清理，或不属于当前用户。

    统一用「找不到」语义，避免区分「不存在」与「不是你的」而泄露信息。
    上层（api/notifications.py）据此返回 404。
    """

    def __init__(self, notification_id=None):
        super().__init__(f"通知不存在或无权访问：{notification_id}")
        self.message = str(self)


class MessageNotFound(Exception):
    """消息不存在、已被删除，或不属于当前用户的会话。

    反馈接口必须按「回答属于谁」鉴权：知识库共享，但对话是私有的，
    任何人都不能给别人的回答打分。上层据此返回 404。
    """

    def __init__(self, message_id=None):
        super().__init__(f"消息不存在或无权访问：{message_id}")
        self.message = str(self)


class FeedbackNotAllowed(Exception):
    """当前消息不允许被评价（例如对用户自己的提问点赞）。

    《需求清单》P6 功能2 的评价对象是「答案」，因此只允许评价 role='assistant'
    的消息；对 user / system 消息提交反馈属于非法调用。上层据此返回 409。

    说明：反馈内容超长（comment 超过上限）同样走本异常——都是
    「入参不被接受」，前端只需展示 message 即可，无需细分错误码。
    """

    def __init__(self, message: str = "该消息不支持评价"):
        super().__init__(message)
        self.message = message
