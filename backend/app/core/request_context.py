# 请求级上下文（P7-B1：结构化日志与审计的公共底座）
# ---------------------------------------------------------------------
# 职责：用 contextvars 保存「当前请求」的元信息（request_id / 用户 / IP / 路径），
#       让日志格式化器、审计服务、限流器在不层层传参的前提下读到同一份上下文。
#
# 为什么用 contextvars 而不是全局变量：
#   FastAPI 的每个请求跑在独立的 asyncio Task 里，contextvars 天然按任务隔离；
#   全局变量在并发下会串号（A 的日志带上 B 的 request_id），排查问题时会误判。
#
# 命名约定：
#   - 本模块只**存取**上下文，不产生业务语义；
#   - 中间件在请求开始时 bind、结束时 clear，业务代码只读。
#
# 与审计的关系：
#   审计日志需要「谁在什么 IP、什么请求 ID 下做了什么」，这些都在上下文里，
#   因此 services/audit_service 直接从这里取，无需每个调用点重复传参。

from contextvars import ContextVar, Token
from typing import Optional

# 请求唯一 ID：由中间件生成（或透传上游网关的 X-Request-ID），用于串联一条请求的全部日志
_request_id: ContextVar[str] = ContextVar("request_id", default="")
# 当前登录用户 ID（鉴权依赖解析成功后写入；未登录为空）
_user_id: ContextVar[str] = ContextVar("user_id", default="")
# 当前登录用户名（审计需要可读身份，避免只存 UUID 无法追溯）
_username: ContextVar[str] = ContextVar("username", default="")
# 客户端 IP（优先取 X-Forwarded-For 第一段，即真实客户端；不存在时取直连地址）
_client_ip: ContextVar[str] = ContextVar("client_ip", default="")
# User-Agent（审计留痕：区分浏览器/脚本/探针）
_user_agent: ContextVar[str] = ContextVar("user_agent", default="")


def bind_request_context(
    request_id: str,
    client_ip: str = "",
    user_agent: str = "",
) -> None:
    """在请求开始时写入上下文（由可观测性中间件调用）。

    Args:
        request_id: 本次请求的唯一 ID。
        client_ip: 客户端 IP（可为空）。
        user_agent: 客户端 UA（可为空，超长会被截断到 512 字符）。
    """
    _request_id.set(request_id or "")
    _client_ip.set(client_ip or "")
    # UA 可能很长，截断避免把日志/审计字段撑爆（数据库列为 VARCHAR(512)）
    _user_agent.set((user_agent or "")[:512])


def bind_user(user_id: str, username: str = "") -> None:
    """鉴权成功后写入当前用户信息（由鉴权依赖或登录接口调用）。

    说明：鉴权依赖是「按需执行」的——不是所有路由都会解析用户，
    因此用户信息只能在校验成功后补写，不能在中间件里预先填充。

    Args:
        user_id: 用户 UUID 字符串。
        username: 用户名（审计展示用，可空）。
    """
    _user_id.set(str(user_id) if user_id else "")
    _username.set(username or "")


def clear_request_context() -> None:
    """请求结束时清空上下文，避免线程/任务复用导致串号。"""
    _request_id.set("")
    _user_id.set("")
    _username.set("")
    _client_ip.set("")
    _user_agent.set("")


def get_request_id() -> str:
    """当前请求 ID（无上下文时返回空串，日志里表现为 '-'）。"""
    return _request_id.get()


def get_user_id() -> str:
    """当前请求的登录用户 ID（未登录返回空串）。"""
    return _user_id.get()


def get_username() -> str:
    """当前请求的登录用户名（未登录返回空串）。"""
    return _username.get()


def get_client_ip() -> str:
    """当前请求的客户端 IP（未知返回空串）。"""
    return _client_ip.get()


def get_user_agent() -> str:
    """当前请求的 User-Agent（未知返回空串）。"""
    return _user_agent.get()


def snapshot() -> dict:
    """一次性取出全部上下文字段（审计写库时使用）。

    Returns:
        形如 {"request_id": ..., "user_id": ..., "username": ..., "ip": ..., "user_agent": ...}。
    """
    return {
        "request_id": _request_id.get(),
        "user_id": _user_id.get(),
        "username": _username.get(),
        "ip": _client_ip.get(),
        "user_agent": _user_agent.get(),
    }


def extract_client_ip(
    x_forwarded_for: Optional[str],
    x_real_ip: Optional[str],
    peer_ip: Optional[str],
) -> str:
    """从代理头中解析真实客户端 IP。

    解析顺序（与 nginx 的 `$proxy_add_x_forwarded_for` 约定一致）：
      1. X-Forwarded-For 的第一段：最靠近客户端的地址，由前置代理追加；
      2. X-Real-IP：部分网关只设置这一个头；
      3. TCP 直连地址（未经代理时的真实来源）。

    安全说明：这些头都可以被客户端伪造，因此**只能**用于日志/审计留痕，
    绝不能用于鉴权或访问控制。

    Args:
        x_forwarded_for: X-Forwarded-For 头原值（可能为 "ip1, ip2"）。
        x_real_ip: X-Real-IP 头原值。
        peer_ip: TCP 对端地址。

    Returns:
        解析出的 IP 字符串；全部为空时返回空串。
    """
    if x_forwarded_for:
        # 取第一段并去空白：后续段是各级代理地址，对追溯来源无意义
        first = x_forwarded_for.split(",")[0].strip()
        if first:
            return first[:64]
    if x_real_ip:
        return x_real_ip.strip()[:64]
    return (peer_ip or "")[:64]


def reset_token(token: Token) -> None:
    """按 token 精确回滚上下文（供需要嵌套绑定/恢复的场景使用）。"""
    try:
        # reset 只能在同一个 Context 中调用，失败时降级为整体清理，绝不抛给业务
        _request_id.reset(token)
    except (ValueError, RuntimeError):
        clear_request_context()
