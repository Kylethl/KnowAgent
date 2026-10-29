# P7 备份服务测试
# ---------------------------------------------------------------------
# 覆盖《需求清单》P7 功能1「PostgreSQL 定期备份（pg_dump / 快照）」的实现。
#
# 关键点：**不真的连数据库**。用一个假 pg_dump 脚本输出合法 dump 文本，
#   从而完整验证「流式压缩 → 校验 → manifest → 保留清理」这条链路，
#   同时保持测试快速、可在任何机器上跑。
#
# 为什么这点值得单独测：
#   备份是「平时没人看、出事必须对」的功能。只验证「函数不报错」没有意义，
#   必须验证「产出的文件真的能通过完整性校验」——否则恢复时才发现是空壳。

import gzip
import os
import shutil
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import get_settings
from app.core.errors import BackupError
from app.services import backup_service

# 测试用工作目录：放在仓库内（沙箱环境禁止写 /tmp）
_WORK_ROOT = Path(__file__).resolve().parent / ".pytest_p7_work"


@pytest.fixture()
def work_dir():
    """提供隔离的临时目录（备份目录、假 pg_dump 都放这里）。"""
    target = _WORK_ROOT / uuid4().hex
    target.mkdir(parents=True, exist_ok=True)
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


@pytest.fixture()
def backup_settings(monkeypatch, work_dir):
    """把备份配置指向临时目录，避免污染真实的 data/backups。"""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "BACKUP_ENABLED", True)
    monkeypatch.setattr(cfg, "BACKUP_DIR", str(work_dir / "backups"))
    monkeypatch.setattr(cfg, "BACKUP_RETENTION_DAYS", 14)
    monkeypatch.setattr(cfg, "BACKUP_RETENTION_COUNT", 30)
    monkeypatch.setattr(
        cfg, "DATABASE_URL", "postgresql+asyncpg://knowagent:pwd@localhost:5432/knowagent"
    )
    return cfg


def _make_fake_pg_dump(directory: Path, exit_code: int = 0, body: str = "SELECT 1;") -> str:
    """生成一个假的 pg_dump 脚本（输出合法 dump 文本，或按需返回失败）。

    Args:
        directory: 脚本所在目录。
        exit_code: 退出码（0 成功，非 0 用于验证失败路径）。
        body: 输出中的 SQL 内容。

    Returns:
        脚本的绝对路径。
    """
    script = directory / "fake_pg_dump.sh"
    script.write_text(
        "#!/bin/sh\n"
        'echo "-- PostgreSQL database dump"\n'
        f'echo "{body}"\n'
        'echo "-- PostgreSQL database dump complete"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


# ---------------------------------------------------------------------
# DSN 解析
# ---------------------------------------------------------------------


def test_parse_dsn_strips_async_driver(monkeypatch):
    """SQLAlchemy 的 +asyncpg 后缀要去掉，否则 pg_dump 不认这个 scheme。"""
    cfg = get_settings()
    monkeypatch.setattr(
        cfg, "DATABASE_URL", "postgresql+asyncpg://knowagent:pwd@db:5433/knowagent"
    )
    conn = backup_service._parse_database_dsn()
    assert conn["host"] == "db"
    assert conn["port"] == "5433"
    assert conn["user"] == "knowagent"
    assert conn["dbname"] == "knowagent"
    assert conn["password"] == "pwd"


def test_parse_dsn_decodes_url_encoded_password(monkeypatch):
    """口令里的特殊字符是 URL 编码的，必须解码后才能用于认证。"""
    cfg = get_settings()
    monkeypatch.setattr(
        cfg, "DATABASE_URL", "postgresql+asyncpg://u:p%40ss%3Aword@localhost:5432/kb"
    )
    assert backup_service._parse_database_dsn()["password"] == "p@ss:word"


def test_parse_dsn_rejects_invalid_url(monkeypatch):
    """缺少库名/主机时应明确报错（而不是让 pg_dump 报一堆看不懂的信息）。"""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "DATABASE_URL", "sqlite:///./test.db")
    with pytest.raises(BackupError):
        backup_service._parse_database_dsn()


def test_scrub_secret_removes_password():
    """错误信息回传前必须抹掉口令（pg_dump 有时会把连接串原样打印）。"""
    assert backup_service._scrub_secret("auth failed for p@ss", "p@ss") == "auth failed for ***"
    assert backup_service._scrub_secret("", "x") == ""


# ---------------------------------------------------------------------
# 文件校验
# ---------------------------------------------------------------------


def test_verify_backup_accepts_valid_dump(work_dir):
    """含 pg_dump 完成标记的完整 gzip 应通过校验。"""
    target = work_dir / "ok.sql.gz"
    with gzip.open(target, "wb") as gz:
        gz.write(b"-- PostgreSQL database dump\nCREATE TABLE t(i int);\n")
        gz.write(b"-- PostgreSQL database dump complete\n")
    ok, message = backup_service.verify_backup_file(str(target))
    assert ok is True
    assert message == "ok"


def test_verify_backup_rejects_missing_marker(work_dir):
    """缺少完成标记视为导出被中断——这种文件绝不能当备份用。"""
    target = work_dir / "partial.sql.gz"
    with gzip.open(target, "wb") as gz:
        gz.write(b"-- PostgreSQL database dump\nCREATE TABLE t(i int);\n")
    ok, message = backup_service.verify_backup_file(str(target))
    assert ok is False
    assert "完成标记" in message


def test_verify_backup_rejects_corrupted_gzip(work_dir):
    """截断的 gzip 流应被识别为损坏。"""
    target = work_dir / "broken.sql.gz"
    with gzip.open(target, "wb") as gz:
        gz.write(b"x" * 5000)
    # 砍掉尾部，破坏 gzip 校验和
    data = target.read_bytes()
    target.write_bytes(data[: len(data) // 2])
    ok, message = backup_service.verify_backup_file(str(target))
    assert ok is False
    assert "损坏" in message or "完成标记" in message


def test_verify_backup_rejects_empty_and_missing(work_dir):
    """空文件与不存在的文件都要被拒绝。"""
    empty = work_dir / "empty.sql.gz"
    empty.write_bytes(b"")
    assert backup_service.verify_backup_file(str(empty))[0] is False
    assert backup_service.verify_backup_file(str(work_dir / "nope.sql.gz"))[0] is False


# ---------------------------------------------------------------------
# 执行备份
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backup_produces_verified_file_and_manifest(backup_settings, monkeypatch, work_dir):
    """完整跑通：执行 → 压缩落盘 → 校验 → 写 manifest → 返回描述。"""
    monkeypatch.setattr(
        backup_settings, "BACKUP_PG_DUMP_BIN", _make_fake_pg_dump(work_dir)
    )

    item = await backup_service.run_backup(trigger="manual", operator="tester")

    assert item["name"].startswith("knowagent-") and item["name"].endswith(".sql.gz")
    assert item["size_bytes"] > 0
    assert item["verified"] is True
    assert item["trigger"] == "manual"
    assert item["operator"] == "tester"
    assert item["sha256"] and len(item["sha256"]) == 64
    assert "restore_postgres.sh" in item["restore_hint"]

    data_path = Path(backup_settings.backup_dir_abs) / item["name"]
    assert data_path.exists()
    # manifest 边车必须存在，否则状态页看不到 sha256/耗时
    assert data_path.with_name(data_path.name + ".json").exists()


@pytest.mark.asyncio
async def test_run_backup_fails_on_nonzero_exit(backup_settings, monkeypatch, work_dir):
    """pg_dump 非 0 退出应抛 BackupError，且不留下半截文件。"""
    monkeypatch.setattr(
        backup_settings, "BACKUP_PG_DUMP_BIN", _make_fake_pg_dump(work_dir, exit_code=3)
    )

    with pytest.raises(BackupError) as exc:
        await backup_service.run_backup()
    assert "退出码" in str(exc.value)

    backup_dir = Path(backup_settings.backup_dir_abs)
    leftovers = list(backup_dir.glob("knowagent-*.sql.gz")) if backup_dir.exists() else []
    assert leftovers == []


@pytest.mark.asyncio
async def test_run_backup_rejects_missing_pg_dump(backup_settings, monkeypatch):
    """找不到 pg_dump 时应给出「装哪个包」的可执行建议，而不是模糊报错。"""
    monkeypatch.setattr(backup_settings, "BACKUP_PG_DUMP_BIN", "definitely-not-exists-xyz")
    with pytest.raises(BackupError) as exc:
        await backup_service.run_backup()
    assert "pg_dump" in str(exc.value)


@pytest.mark.asyncio
async def test_run_backup_rejects_when_disabled(backup_settings, monkeypatch):
    """关闭备份开关后，手动触发也应被拒绝（避免运维误操作）。"""
    monkeypatch.setattr(backup_settings, "BACKUP_ENABLED", False)
    with pytest.raises(BackupError) as exc:
        await backup_service.run_backup()
    assert "未启用" in str(exc.value)


# ---------------------------------------------------------------------
# 列表 / 保留 / 总览
# ---------------------------------------------------------------------


def test_list_backups_sorted_desc_and_reads_manifest(backup_settings, work_dir):
    """列表按时间倒序，且能读出 manifest 里的 sha256 等字段。"""
    backup_dir = Path(backup_settings.backup_dir_abs)
    backup_dir.mkdir(parents=True, exist_ok=True)

    old = backup_dir / "knowagent-20260101-030000.sql.gz"
    new = backup_dir / "knowagent-20261028-030000.sql.gz"
    for path in (old, new):
        with gzip.open(path, "wb") as gz:
            gz.write(b"-- PostgreSQL database dump complete\n")
    # 显式设置修改时间，保证排序断言稳定
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))
    (new.with_name(new.name + ".json")).write_text(
        '{"name": "%s", "sha256": "deadbeef", "verified": true}' % new.name,
        encoding="utf-8",
    )

    items = backup_service.list_backups()
    assert [item["name"] for item in items] == [new.name, old.name]
    assert items[0]["sha256"] == "deadbeef"
    assert items[0]["verified"] is True


def test_prune_backups_keeps_only_retention_count(backup_settings, monkeypatch):
    """按个数清理：只保留最近的 N 个（同步删除 manifest）。"""
    monkeypatch.setattr(backup_settings, "BACKUP_RETENTION_COUNT", 2)
    monkeypatch.setattr(backup_settings, "BACKUP_RETENTION_DAYS", 0)

    backup_dir = Path(backup_settings.backup_dir_abs)
    backup_dir.mkdir(parents=True, exist_ok=True)
    for index in range(4):
        path = backup_dir / f"knowagent-2026010{index + 1}-030000.sql.gz"
        path.write_bytes(b"x")
        path.with_name(path.name + ".json").write_text("{}", encoding="utf-8")
        # 时间递增，保证「最新两个」判定明确
        os.utime(path, (1_700_000_000 + index * 100, 1_700_000_000 + index * 100))

    removed = backup_service.prune_backups()
    assert removed == 2
    remaining = sorted(p.name for p in backup_dir.glob("knowagent-*.sql.gz"))
    assert remaining == [
        "knowagent-20260103-030000.sql.gz",
        "knowagent-20260104-030000.sql.gz",
    ]
    # manifest 也要一起删，否则会留下指向不存在文件的孤儿文件
    assert not (backup_dir / "knowagent-20260101-030000.sql.gz.json").exists()


def test_prune_backups_noop_when_dir_missing(backup_settings, monkeypatch, work_dir):
    """目录不存在时清理应返回 0（而不是抛异常）。"""
    monkeypatch.setattr(backup_settings, "BACKUP_DIR", str(work_dir / "not-exist"))
    assert backup_service.prune_backups() == 0


def test_backup_status_reports_latest_and_limits(backup_settings):
    """总览应包含开关、保留策略、下次执行提示与最近一次备份。"""
    status = backup_service.backup_status()
    assert status["enabled"] is True
    assert status["retention_days"] == 14
    assert status["next_run_hint"] is not None
    assert status["last"] is None  # 尚无备份
    assert status["last_verified"] is False


def test_format_bytes():
    """字节格式化用于日志与 CLI 输出，边界值不能错。"""
    assert backup_service.format_bytes(0) == "0B"
    assert backup_service.format_bytes(512) == "512B"
    assert backup_service.format_bytes(2048) == "2.0KB"
    assert backup_service.format_bytes(5 * 1024 * 1024).endswith("MB")
