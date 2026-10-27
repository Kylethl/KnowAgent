# 用户认证服务（P2-B2：注册 / 登录 / JWT / bcrypt）
# ---------------------------------------------------------------------
# 职责：把「密码哈希与校验」「用户注册」「登录认证」「JWT 签发与解析」
#       收敛到一处，供 api/auth.py（注册登录接口）与 core/security.py
#       （鉴权依赖）复用，避免加解密逻辑散落在路由层。
#
# 设计要点：
#   - 密码绝不存明文：一律用 bcrypt 加盐哈希后写入 users.password_hash
#     （《需求清单》P2-B2 明确要求 bcrypt）。
#   - 登录失败不区分「用户名不存在」与「密码错误」，统一返回同一错误，
#     避免被攻击者用于枚举已注册用户名。
#   - JWT 为无状态令牌：payload 只放 sub（用户 ID）/ username / role / exp / iat，
#     不放敏感信息（令牌可被解码，只是不能篡改）。
#   - 令牌不落库、无刷新令牌：过期后重新登录即可（P2 阶段约定；后续可加 refresh token）。

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID

import bcrypt
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import (
    InvalidCredentials,
    UnauthorizedError,
    UserAlreadyExists,
    WeakPasswordError,
)
from ..core.logging import get_logger
from ..db.models import User

logger = get_logger(__name__)

# ---------------------------------------------------------------------
# 密码规则（注册时校验）
# ---------------------------------------------------------------------
# 最短长度：8 位；同时要求「字母 + 数字」组合，避免纯字典弱口令。
_MIN_PASSWORD_LENGTH = 8
# 最长长度：72 字节。这不是业务限制，而是 bcrypt 算法的硬上限——
# 超过 72 字节的部分会被静默截断（bcrypt 5.x 会直接报错），故在此提前拒绝。
_MAX_PASSWORD_BYTES = 72


def _get_settings():
    """延迟获取配置（避免模块导入期就读取 .env，便于单测覆盖）。

    Returns:
        全局 Settings 实例。
    """
    from ..config import get_settings

    return get_settings()


def hash_password(plain_password: str) -> str:
    """用 bcrypt 对明文密码做加盐哈希，返回可安全入库的字符串。

    bcrypt 每次会自动生成随机盐，因此同一密码两次哈希结果不同（防彩虹表）。

    Args:
        plain_password: 明文密码。

    Returns:
        bcrypt 哈希字符串（形如 $2b$12$...），直接存 users.password_hash。

    Raises:
        ValueError: 密码超过 bcrypt 的 72 字节上限。
    """
    cfg = _get_settings()
    # bcrypt 只接受 bytes；按 UTF-8 编码后哈希
    password_bytes = plain_password.encode("utf-8")
    if len(password_bytes) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"密码长度不能超过 {_MAX_PASSWORD_BYTES} 字节（bcrypt 算法上限）")
    # gensalt(rounds=...)：cost factor，越大越慢越抗暴力破解（默认 12）
    salt = bcrypt.gensalt(rounds=cfg.BCRYPT_ROUNDS)
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验明文密码是否与库中 bcrypt 哈希匹配。

    Args:
        plain_password: 用户登录时输入的明文密码。
        password_hash: 库中存储的 bcrypt 哈希。

    Returns:
        True 表示密码正确；False 表示不匹配（含哈希损坏等异常情况）。
    """
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # 哈希格式非法（如历史脏数据）：按「校验失败」处理，不抛异常给上层
        logger.warning("密码哈希格式非法，校验按失败处理")
        return False


def validate_password_strength(plain_password: str) -> None:
    """注册前校验密码强度，不满足规则直接抛错。

    规则：
      1. 长度 8~72 字节（72 为 bcrypt 硬上限）；
      2. 至少包含一个字母和一个数字。

    Args:
        plain_password: 待校验的明文密码。

    Raises:
        WeakPasswordError: 不满足上述任一规则（message 含具体原因，供前端展示）。
    """
    if not plain_password:
        raise WeakPasswordError("密码不能为空")

    password_bytes = plain_password.encode("utf-8")
    if len(password_bytes) < _MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"密码长度不能少于 {_MIN_PASSWORD_LENGTH} 位")
    if len(password_bytes) > _MAX_PASSWORD_BYTES:
        raise WeakPasswordError(f"密码长度不能超过 {_MAX_PASSWORD_BYTES} 字节")

    # 组合复杂度：至少 1 个字母 + 1 个数字
    has_letter = any(ch.isalpha() for ch in plain_password)
    has_digit = any(ch.isdigit() for ch in plain_password)
    if not (has_letter and has_digit):
        raise WeakPasswordError("密码需同时包含字母和数字")


async def get_user_by_username(db: AsyncSession, username: str) -> Optional[User]:
    """按用户名查询用户（注册查重与登录取用户共用）。

    Args:
        db: 数据库会话。
        username: 登录名（唯一）。

    Returns:
        User 对象；不存在时返回 None。
    """
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    """按邮箱查询用户（注册查重用；邮箱可为空，故仅在传入时调用）。

    Args:
        db: 数据库会话。
        email: 邮箱地址（唯一）。

    Returns:
        User 对象；不存在时返回 None。
    """
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> Optional[User]:
    """按主键查询用户（JWT 解析出 sub 后取用户对象）。

    Args:
        db: 数据库会话。
        user_id: 用户 UUID。

    Returns:
        User 对象；不存在时返回 None。
    """
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def register_user(
    db: AsyncSession,
    username: str,
    password: str,
    email: Optional[str] = None,
    role: str = "user",
) -> User:
    """注册新用户：校验强度 → 查重 → bcrypt 哈希 → 落库。

    Args:
        db: 数据库会话。
        username: 登录名（唯一）。
        password: 明文密码（函数内部哈希，绝不落库明文）。
        email: 可选邮箱（唯一）。
        role: 角色，默认 'user'；
              ⚠️ 本参数仅供内部/后续 admin 开通使用，公开注册接口强制 'user'，
              防止客户端自行提权为 admin（P3 RBAC 安全基线）。

    Returns:
        新建的 User 对象（已 flush，可拿到生成的 id）。

    Raises:
        WeakPasswordError: 密码强度不足。
        UserAlreadyExists: 用户名或邮箱已被占用。
    """
    # 1) 密码强度校验（放最前，避免为弱口令白白查库）
    validate_password_strength(password)

    # 2) 用户名查重
    if await get_user_by_username(db, username) is not None:
        raise UserAlreadyExists(field="username")

    # 3) 邮箱查重（仅在填写邮箱时校验）
    if email:
        if await get_user_by_email(db, email) is not None:
            raise UserAlreadyExists(field="email")

    # 4) 哈希密码并落库（role 由服务端决定，不信客户端传入的提权）
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role="user" if role not in ("user", "admin") else role,
    )
    db.add(user)
    # flush 立即拿到 PG 生成的 UUID，便于返回给前端与签发令牌
    await db.flush()
    logger.info("新用户注册成功：username=%s，id=%s，role=%s", username, user.id, user.role)
    return user


async def authenticate_user(db: AsyncSession, username: str, password: str) -> User:
    """登录认证：校验用户名与密码，成功返回用户对象。

    安全约定：用户名不存在与密码错误统一抛 InvalidCredentials，
    不在错误信息中区分，防止用户名枚举。

    Args:
        db: 数据库会话。
        username: 登录名。
        password: 明文密码。

    Returns:
        认证通过的 User 对象。

    Raises:
        InvalidCredentials: 用户名不存在 / 密码错误 / 账号已被禁用。
    """
    user = await get_user_by_username(db, username)
    if user is None:
        # 用户名不存在：仍走一次哈希校验（耗时接近），避免用响应时间侧信道枚举用户
        hash_password(password)
        raise InvalidCredentials()

    if not verify_password(password, user.password_hash):
        raise InvalidCredentials()

    # 账号被软封禁（users.is_active=False）时拒绝登录
    if not user.is_active:
        logger.info("用户已被禁用，拒绝登录：username=%s", username)
        raise InvalidCredentials("账号已被禁用，请联系管理员")

    return user


# =====================================================================
# JWT 签发与解析
# =====================================================================


def create_access_token(user: User, expires_minutes: Optional[int] = None) -> str:
    """为用户签发 JWT 访问令牌。

    payload 约定：
      - sub：用户 ID（JWT 标准字段，字符串形式）
      - username / role：便于前端展示与后续 RBAC 判断
      - iat / exp：签发时间与过期时间（UTC）

    Args:
        user: 用户对象。
        expires_minutes: 有效期（分钟）；不传则用配置 JWT_EXPIRE_MINUTES。

    Returns:
        编码后的 JWT 字符串（形如 xxx.yyy.zzz）。
    """
    cfg = _get_settings()
    # 过期时间：默认取配置，允许调用方覆盖（便于单测用短时效令牌）
    minutes = expires_minutes if expires_minutes is not None else cfg.JWT_EXPIRE_MINUTES
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=minutes)

    payload: Dict[str, Any] = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": expire,
    }
    return jwt.encode(payload, cfg.JWT_SECRET_KEY, algorithm=cfg.JWT_ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """解析并校验 JWT，返回 payload。

    校验项：签名是否合法、是否过期、算法是否为配置允许的算法。

    Args:
        token: 前端传来的 JWT 字符串（不含 'Bearer ' 前缀）。

    Returns:
        解码后的 payload 字典。

    Raises:
        UnauthorizedError: 令牌过期 / 签名不合法 / 格式错误。
    """
    cfg = _get_settings()
    try:
        return jwt.decode(
            token,
            cfg.JWT_SECRET_KEY,
            # 显式限定算法白名单，防止 alg=none 等降级攻击
            algorithms=[cfg.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("登录已过期，请重新登录")
    except jwt.InvalidTokenError as exc:
        # 覆盖签名错误、格式错误、audience 不符等全部非法令牌
        logger.debug("JWT 解析失败：%s", exc)
        raise UnauthorizedError("登录令牌无效，请重新登录")


async def get_user_from_token(db: AsyncSession, token: str) -> User:
    """由 JWT 反查当前用户（鉴权依赖的核心逻辑）。

    流程：解析令牌 → 取 sub（用户 ID）→ 查库 → 校验账号是否仍启用。

    Args:
        db: 数据库会话。
        token: JWT 字符串。

    Returns:
        当前登录用户对象。

    Raises:
        UnauthorizedError: 令牌无效 / 用户已不存在 / 账号已被禁用。
    """
    payload = decode_access_token(token)

    # sub 是 JWT 标准字段，存的是用户 ID 字符串
    sub = payload.get("sub")
    if not sub:
        raise UnauthorizedError("登录令牌无效，请重新登录")
    try:
        user_id = UUID(str(sub))
    except (ValueError, AttributeError):
        raise UnauthorizedError("登录令牌无效，请重新登录")

    user = await get_user_by_id(db, user_id)
    if user is None:
        # 令牌有效但用户已被删除（如清库后旧令牌）
        raise UnauthorizedError("用户不存在，请重新登录")
    if not user.is_active:
        # 令牌签发后账号被禁用：立即失效，不等令牌自然过期
        raise UnauthorizedError("账号已被禁用，请联系管理员")

    return user
