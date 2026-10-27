# 管理员账号运维工具（P3-B1 前置）
# ---------------------------------------------------------------------
# 职责：为 RBAC 提供**唯一受控的**管理员开通入口。
#
# 为什么需要它：
#   《需求清单》P2-B2 的安全基线是「注册接口强制 role='user'」，客户端无法提权；
#   但这也意味着**没有任何正常途径能产生 admin**。此前想加管理员只能手写 SQL
#   或临时脚本，容易漏掉 bcrypt 哈希 / 强度校验，埋下弱口令与明文风险。
#   本脚本把这条运维路径固化下来，保证与注册链路走同一套密码规则。
#
# 用法（均在 backend/ 目录下执行）：
#   python scripts/create_admin.py --username admin
#       # 新建管理员；未给 --password 时随机生成强密码并**仅打印一次**
#   python scripts/create_admin.py --username admin --password 'Admin@12345'
#       # 新建管理员并指定密码（不建议在 shell 历史里留明文，优先用随机生成）
#   python scripts/create_admin.py --username alice --promote
#       # 把已存在的普通用户提升为管理员（不改密码）
#   python scripts/create_admin.py --username admin --password 'New@12345' --reset-password
#       # 重置已有管理员的密码
#
# 幂等性：
#   - 用户不存在 → 新建为 admin；
#   - 用户已存在且已是 admin → 不改动密码，直接退出（退出码 0）；
#   - 用户已存在但非 admin → 默认**拒绝**改动，必须显式加 --promote，
#     避免误把普通账号提权（最小权限原则）。
#
# 安全约定：
#   - 密码一律经 auth_service 的 bcrypt 哈希 + 强度校验，绝不存明文；
#   - 随机密码只用 secrets 生成，打印到终端后不再出现在任何日志中。

from __future__ import annotations

import argparse
import secrets
import string
import sys
from pathlib import Path

# 让脚本既能 `python scripts/create_admin.py` 直接跑，
# 也能 `python -m scripts.create_admin` 以模块方式跑：把 backend/ 根目录加入模块搜索路径。
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import asyncio  # noqa: E402 —— 必须在 sys.path 调整后导入项目模块

from sqlalchemy import select  # noqa: E402

from app.core.logging import get_logger  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.db.models import User  # noqa: E402
from app.services.auth_service import (  # noqa: E402
    hash_password,
    validate_password_strength,
)

logger = get_logger(__name__)

# 随机密码长度：16 位，兼顾强度与可复制性
_RANDOM_PASSWORD_LENGTH = 16


def generate_strong_password(length: int = _RANDOM_PASSWORD_LENGTH) -> str:
    """生成满足强度规则的随机密码（字母 + 数字，长度达标）。

    用 secrets（密码学安全随机源），不用 random——后者可被预测。

    Args:
        length: 密码长度，默认 16。

    Returns:
        随机密码字符串；保证至少含 1 个字母与 1 个数字。
    """
    alphabet = string.ascii_letters + string.digits
    while True:
        # 逐字符选取，避免字符集偏斜
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        # 兜底校验：确保同时含字母与数字（满足 validate_password_strength 规则）
        if any(c.isalpha() for c in candidate) and any(c.isdigit() for c in candidate):
            return candidate


async def _get_user_by_username(username: str) -> User | None:
    """按用户名查询用户。

    Args:
        username: 登录名。

    Returns:
        User 对象；不存在时返回 None。
    """
    async with SessionLocal() as db:
        result = await db.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()


async def create_or_promote_admin(
    username: str,
    password: str | None = None,
    promote: bool = False,
    reset_password: bool = False,
) -> tuple[User, str | None]:
    """创建管理员 / 提升现有用户为管理员（幂等）。

    Args:
        username: 管理员登录名。
        password: 明文密码；为 None 时自动生成强密码。
        promote: 用户已存在且非 admin 时，是否允许提升为 admin。
        reset_password: 用户已存在时，是否重设其密码。

    Returns:
        (user, plain_password) 二元组：
          plain_password 仅当「新建」或「重设密码」时有值（用于打印一次），
          其余情况为 None（不改动密码，也无从得知原密码）。

    Raises:
        SystemExit: 安全策略拒绝（如已存在普通用户但未加 --promote）。
        ValueError: 密码强度不足（来自 validate_password_strength）。
    """
    existing = await _get_user_by_username(username)

    # ---------- 分支一：用户不存在 → 新建管理员 ----------
    if existing is None:
        # 未指定密码则生成强密码（保证不产生弱口令管理员）
        plain = password or generate_strong_password()
        # 与注册链路同一套强度规则（8~72 字节 + 字母数字组合）
        validate_password_strength(plain)

        async with SessionLocal() as db:
            user = User(
                username=username,
                password_hash=hash_password(plain),
                # 角色由服务端（本脚本）决定为 admin，这是唯一合法的提权入口
                role="admin",
                is_active=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
        logger.info("已创建管理员账号：username=%s，id=%s", user.username, user.id)
        return user, plain

    # ---------- 分支二：用户已存在 ----------
    if existing.role != "admin" and not promote:
        # 安全兜底：默认不允许把已存在的普通账号悄悄提权，必须显式 --promote
        print(
            f"用户「{username}」已存在且角色为 {existing.role!r}。"
            f"如确认要提升为管理员，请加 --promote 参数。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    changed = False
    plain_printed: str | None = None

    async with SessionLocal() as db:
        # 重新在会话内取一次，保证后续写入发生在同一会话中
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if user is None:  # 理论上不会发生（刚查到过），防御并发删除
            raise SystemExit(3)

        # 提升角色
        if user.role != "admin":
            user.role = "admin"
            changed = True

        # 重设密码（仅在显式要求，或新建时未指定密码的等价场景）
        if reset_password:
            plain = password or generate_strong_password()
            validate_password_strength(plain)
            user.password_hash = hash_password(plain)
            plain_printed = plain
            changed = True

        if changed:
            await db.commit()
            await db.refresh(user)
            logger.info(
                "管理员账号已更新：username=%s，role=%s，重设密码=%s",
                user.username, user.role, bool(plain_printed),
            )
        else:
            # 已是 admin 且未要求改密码 → 幂等无操作
            print(f"用户「{username}」已是管理员，未做任何改动（幂等退出）。")

    return user, plain_printed


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="管理员账号运维工具：创建 / 提升 / 重置密码（P3 RBAC）"
    )
    parser.add_argument(
        "--username", required=True, help="管理员登录名（唯一）"
    )
    parser.add_argument(
        "--password",
        default=None,
        help="明文密码；不传则自动生成强密码并打印一次（推荐）",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="用户已存在且非 admin 时，允许提升为管理员",
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="用户已存在时，重设其密码（不传 --password 则自动生成）",
    )
    return parser.parse_args()


def main() -> None:
    """命令行入口：执行创建/提升并打印结果凭据。"""
    args = _parse_args()

    user, plain = asyncio.run(
        create_or_promote_admin(
            username=args.username,
            password=args.password,
            promote=args.promote,
            reset_password=args.reset_password,
        )
    )

    # 结果输出：密码只在此处打印一次，绝不写入日志
    print("=" * 56)
    print("管理员账号就绪")
    print(f"  用户名：{user.username}")
    print(f"  角色  ：{user.role}")
    print(f"  用户ID：{user.id}")
    if plain:
        print(f"  密码  ：{plain}   （请立即保存，此处只显示一次）")
    else:
        print("  密码  ：（未改动，沿用原密码）")
    print("=" * 56)


if __name__ == "__main__":
    main()
