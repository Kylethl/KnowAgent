# 用户鉴权测试（P2-B2：bcrypt / JWT / 注册 / 登录 / 鉴权拦截）
#
# 原则（见《测试任务清单》）：不依赖真实数据库与网络，用 AsyncMock 替代会话，
#       聚焦验证「密码哈希与校验规则」「JWT 签发/解析/防伪造」「注册查重」
#       「登录失败统一文案」「未携带令牌时接口返回 401」。

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import app.api.conversations  # noqa: F401 —— 注册路由，保证 /conversations 参与鉴权测试
from app.core.errors import (
    InvalidCredentials,
    UnauthorizedError,
    UserAlreadyExists,
    WeakPasswordError,
)
from app.core.security import DEMO_USER_ID, get_user_for_request
from app.db.engine import get_db
from app.db.models import User
from app.main import create_app
from app.services import auth_service

# 测试用明文密码（满足强度规则：含字母与数字、长度 ≥8）
_TEST_PASSWORD = "Test12345"


def _make_user(
    username: str = "alice",
    password: str = _TEST_PASSWORD,
    role: str = "user",
    is_active: bool = True,
) -> User:
    """构造一个内存中的用户对象（密码按真实 bcrypt 规则哈希）。"""
    user = User()
    user.id = uuid4()
    user.username = username
    user.password_hash = auth_service.hash_password(password)
    user.role = role
    user.is_active = is_active
    user.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return user


def _make_db(existing_user=None) -> AsyncMock:
    """构造 AsyncMock 会话：db.execute 的结果按 existing_user 返回单行或空。

    Args:
        existing_user: 让「按用户名/ID 查询」返回的用户；None 表示查不到。
    """
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing_user
    db.execute.return_value = result
    return db


# =====================================================================
# 一、密码哈希与强度校验
# =====================================================================


def test_password_hashed_with_bcrypt_and_verifiable():
    """密码应被 bcrypt 加盐哈希：不等于明文，且可正确校验。"""
    hashed = auth_service.hash_password(_TEST_PASSWORD)
    # bcrypt 哈希形如 $2b$12$...（不同版本前缀可能是 2a）
    assert hashed.startswith("$2")
    assert hashed != _TEST_PASSWORD
    assert auth_service.verify_password(_TEST_PASSWORD, hashed) is True


def test_wrong_password_fails_verification():
    """错误密码必须校验失败。"""
    hashed = auth_service.hash_password(_TEST_PASSWORD)
    assert auth_service.verify_password("Wrong12345", hashed) is False


def test_weak_passwords_are_rejected():
    """弱密码（过短 / 无数字 / 无字母）应在注册前被拒绝。"""
    with pytest.raises(WeakPasswordError):
        auth_service.validate_password_strength("abc")  # 过短
    with pytest.raises(WeakPasswordError):
        auth_service.validate_password_strength("abcdefgh")  # 无数字
    with pytest.raises(WeakPasswordError):
        auth_service.validate_password_strength("12345678")  # 无字母


def test_valid_password_passes_strength_check():
    """合规密码（含字母与数字、长度达标）不应抛错。"""
    auth_service.validate_password_strength(_TEST_PASSWORD)  # 不抛异常即通过


# =====================================================================
# 二、JWT 签发与解析
# =====================================================================


def test_jwt_roundtrip_carries_user_claims():
    """签发的令牌应能解出 sub / username / role。"""
    user = _make_user()
    token = auth_service.create_access_token(user)
    payload = auth_service.decode_access_token(token)
    assert payload["sub"] == str(user.id)
    assert payload["username"] == user.username
    assert payload["role"] == user.role


def test_expired_token_is_rejected():
    """过期令牌应解析失败并抛未认证异常。"""
    user = _make_user()
    # 有效期设为 -1 分钟 → 立即过期
    token = auth_service.create_access_token(user, expires_minutes=-1)
    with pytest.raises(UnauthorizedError):
        auth_service.decode_access_token(token)


def test_malformed_token_is_rejected():
    """格式非法的令牌不应被接受。"""
    with pytest.raises(UnauthorizedError):
        auth_service.decode_access_token("not-a-jwt")


def test_forged_token_signed_with_wrong_key_is_rejected():
    """用错误密钥伪造的令牌必须被拒绝（签名校验生效）。"""
    user = _make_user()
    forged = pyjwt.encode(
        {"sub": str(user.id), "username": user.username, "role": "admin"},
        "attacker-secret-key-attacker-secret-key",  # 与配置密钥不同
        algorithm="HS256",
    )
    with pytest.raises(UnauthorizedError):
        auth_service.decode_access_token(forged)


# =====================================================================
# 三、注册与登录（服务层）
# =====================================================================


@pytest.mark.asyncio
async def test_register_rejects_duplicate_username():
    """用户名已存在时应抛冲突异常（409 的领域源头）。"""
    db = _make_db(existing_user=_make_user())
    with pytest.raises(UserAlreadyExists):
        await auth_service.register_user(db, "alice", _TEST_PASSWORD)


@pytest.mark.asyncio
async def test_register_hashes_password_and_defaults_role():
    """注册成功应写入 bcrypt 哈希（非明文），角色默认为 user。"""
    db = _make_db(existing_user=None)
    user = await auth_service.register_user(db, "bob", _TEST_PASSWORD)
    assert user.password_hash != _TEST_PASSWORD  # 绝不存明文
    assert auth_service.verify_password(_TEST_PASSWORD, user.password_hash)  # 可校验
    assert user.role == "user"


@pytest.mark.asyncio
async def test_login_with_wrong_password_fails():
    """密码错误应抛统一失败异常（不区分用户不存在与密码错误）。"""
    db = _make_db(existing_user=_make_user())
    with pytest.raises(InvalidCredentials):
        await auth_service.authenticate_user(db, "alice", "Wrong12345")


@pytest.mark.asyncio
async def test_login_with_unknown_user_fails_with_same_error():
    """用户名不存在应抛同一异常，避免被枚举已注册账号。"""
    db = _make_db(existing_user=None)
    with pytest.raises(InvalidCredentials):
        await auth_service.authenticate_user(db, "nobody", _TEST_PASSWORD)


@pytest.mark.asyncio
async def test_disabled_user_cannot_login():
    """被禁用账号（is_active=False）不得登录。"""
    db = _make_db(existing_user=_make_user(is_active=False))
    with pytest.raises(InvalidCredentials):
        await auth_service.authenticate_user(db, "alice", _TEST_PASSWORD)


@pytest.mark.asyncio
async def test_login_success_returns_user():
    """凭据正确时应返回用户对象。"""
    user = _make_user()
    db = _make_db(existing_user=user)
    result = await auth_service.authenticate_user(db, "alice", _TEST_PASSWORD)
    assert result is user


# =====================================================================
# 四、鉴权依赖：令牌 → 用户
# =====================================================================


@pytest.mark.asyncio
async def test_missing_token_is_rejected_when_auth_required():
    """开启鉴权时，未提供令牌应抛未认证异常（→ 401）。"""
    db = _make_db()
    with pytest.raises(UnauthorizedError):
        await get_user_for_request(db, None)


@pytest.mark.asyncio
async def test_get_user_from_token_loads_user():
    """有效令牌应反查出对应用户。"""
    user = _make_user()
    token = auth_service.create_access_token(user)
    db = _make_db(existing_user=user)
    loaded = await auth_service.get_user_from_token(db, token)
    assert loaded is user


@pytest.mark.asyncio
async def test_auth_disabled_falls_back_to_demo_user(monkeypatch):
    """AUTH_REQUIRED=false（本地联调）时应回退到 Demo 用户。"""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "AUTH_REQUIRED", False)

    db = _make_db(existing_user=None)  # 库中没有 Demo 用户 → 自动创建
    user = await get_user_for_request(db, None)
    assert user.id == DEMO_USER_ID


# =====================================================================
# 五、接口层：/auth 与「未带令牌 → 401」
# =====================================================================


@pytest.fixture()
def client():
    """测试应用：把数据库依赖换成 AsyncMock（不连真实库）。"""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: _make_db()
    return TestClient(app)


def test_register_endpoint_returns_201_and_user(client):
    """POST /auth/register 成功应返回 201 与脱敏用户信息。"""
    resp = client.post(
        "/auth/register", json={"username": "charlie", "password": _TEST_PASSWORD}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "charlie"
    assert body["role"] == "user"
    # 敏感字段绝不能出现在响应里
    assert "password_hash" not in body


def test_register_duplicate_returns_409_with_field(client):
    """重复注册应返回 409，并告知冲突字段便于前端高亮。"""
    db = _make_db(existing_user=_make_user(username="alice"))
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)

    resp = c.post("/auth/register", json={"username": "alice", "password": _TEST_PASSWORD})
    assert resp.status_code == 409
    assert resp.json()["field"] == "username"


def test_login_endpoint_returns_token(client):
    """POST /auth/login 成功应返回令牌、有效期与用户信息。"""
    user = _make_user()
    db = _make_db(existing_user=user)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)

    resp = c.post("/auth/login", json={"username": "alice", "password": _TEST_PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["access_token"]
    assert body["user"]["username"] == "alice"


def test_login_wrong_password_returns_401(client):
    """密码错误应返回 401，且文案不区分「用户不存在」与「密码错误」。"""
    db = _make_db(existing_user=_make_user())
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)

    resp = c.post("/auth/login", json={"username": "alice", "password": "Wrong12345"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "用户名或密码错误"
    # 401 应带标准 WWW-Authenticate 头，提示需要 Bearer 令牌
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_protected_endpoints_require_token():
    """未携带令牌访问受保护接口（/auth/me、/conversations）应一律 401。"""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: _make_db()
    c = TestClient(app)

    assert c.get("/auth/me").status_code == 401
    assert c.get("/conversations").status_code == 401
    assert c.post("/conversations", json={}).status_code == 401
    assert c.delete(f"/conversations/{uuid4()}").status_code == 401


def test_me_returns_user_with_valid_token():
    """携带有效令牌访问 /auth/me 应返回当前用户。"""
    user = _make_user()
    token = auth_service.create_access_token(user)

    app = create_app()
    app.dependency_overrides[get_db] = lambda: _make_db(existing_user=user)
    c = TestClient(app)

    resp = c.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == user.username
