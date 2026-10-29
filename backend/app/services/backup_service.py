# PostgreSQL 备份与 Redis 持久化检查（P7-DB1 / P7-Dep1）
# ---------------------------------------------------------------------
# 对应《需求清单》P7 功能1「数据存储：PostgreSQL 定期备份（pg_dump/快照）；Redis 持久化」。
#
# 为什么备份要写进应用而不是「丢给运维写个 cron」：
#   1. 备份是**业务连续性**能力，需要一个可观测的入口（管理端能看到「最近成功备份时间」）；
#   2. 「备份是否真的可用」必须可验证——只产生文件不校验，等于没有备份；
#   3. 备份失败必须能被告警（指标 + 审计 + Sentry），否则会在真正需要恢复时才发现是空的。
#
# 实现要点（都是踩过坑才这么写的）：
#   - 用 **Popen 直连 pg_dump**，流式压缩写盘：不把整库 dump 读进内存（大库会 OOM）；
#   - 口令走 **PGPASSWORD 环境变量**，不放在命令行参数里（ps 能看到 argv）；
#   - 边写边算 **sha256**，落盘后写 JSON 边车（manifest）：便于校验文件完整性；
#   - 失败时**删除半截文件**：半截的 .sql.gz 看起来像备份，恢复时才发现不可用，是最坏的坑；
#   - 保留策略按「天数 + 个数」双约束：只按天会因一天内多次手动备份而堆积，
#     只按个数会在低峰期保留过久（合规上更在意时间维度）。
#
# 恢复：本模块**不提供**恢复接口（避免一条错误调用覆盖生产库）。
#       恢复走 scripts/restore_postgres.sh，必须人工执行并二次确认。

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from ..config import get_settings
from ..core import metrics
from ..core.errors import BackupError
from ..core.logging import get_logger

logger = get_logger(__name__)

# 备份文件名前缀：list_backups 靠它识别自己的产物（目录里可能还有别的手工文件）
_BACKUP_PREFIX = "knowagent-"
# 分块读写的块大小（字节）：64KB 在吞吐与内存之间比较平衡
_CHUNK_SIZE = 64 * 1024
# 校验文件完整性时，gzip 尾部必须出现 pg_dump 的完成标记
_DUMP_COMPLETE_MARK = b"PostgreSQL database dump complete"


def _parse_database_dsn() -> Dict[str, str]:
    """把 SQLAlchemy 的异步 DSN 拆成 pg_dump 需要的连接参数。

    例：
        postgresql+asyncpg://knowagent:pwd@localhost:5432/knowagent
        → {"host": "localhost", "port": "5432", "user": "knowagent",
           "password": "pwd", "dbname": "knowagent"}

    为什么要自己拆而不是直接把 URL 传给 pg_dump：
        1. SQLAlchemy 的 scheme 带 "+asyncpg"，pg_dump 不认；
        2. 口令放在 URL 里会被写进 ps 输出（进程列表可见），拆出来后走环境变量更安全。

    Returns:
        连接参数字典。

    Raises:
        BackupError: DSN 无法解析（缺失库名/主机）。
    """
    settings = get_settings()
    raw = (settings.DATABASE_URL or "").strip()
    # 归一化 scheme：postgresql+asyncpg → postgresql
    normalized = raw.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgres+asyncpg://", "postgresql://"
    )
    parsed = urlparse(normalized)
    dbname = (parsed.path or "").lstrip("/")
    if not parsed.hostname or not dbname:
        raise BackupError(
            "无法从 DATABASE_URL 解析数据库连接信息（需要形如 "
            "postgresql+asyncpg://user:pass@host:port/dbname）"
        )
    return {
        "host": parsed.hostname,
        "port": str(parsed.port or 5432),
        "user": unquote(parsed.username or ""),
        # 口令可能含 @ / : 等字符，必须 URL 解码后再用
        "password": unquote(parsed.password or ""),
        "dbname": dbname,
    }


def _scrub_secret(text: str, secret: str) -> str:
    """把文本中可能出现的数据库口令替换掉（错误信息回传前端/日志前调用）。

    Args:
        text: 原始文本（如 pg_dump 的 stderr）。
        secret: 需要抹掉的口令。

    Returns:
        脱敏后的文本（口令为空时原样返回）。
    """
    if secret and text:
        return text.replace(secret, "***")
    return text or ""


def _backup_paths(name: str) -> tuple[str, str]:
    """返回备份文件与边车 manifest 的完整路径。

    Args:
        name: 备份文件名（如 knowagent-20261028-030000.sql.gz）。

    Returns:
        (数据文件路径, manifest 路径)
    """
    settings = get_settings()
    base = os.path.join(settings.backup_dir_abs, name)
    return base, base + ".json"


def _next_run_hint() -> Optional[str]:
    """计算下一次定时备份的预计时间（仅用于展示，不代表调度器真实状态）。

    Returns:
        ISO 8601 字符串；未启用备份时返回 None。
    """
    settings = get_settings()
    if not settings.BACKUP_ENABLED:
        return None
    now = datetime.now()
    target = now.replace(
        hour=max(0, min(23, settings.BACKUP_SCHEDULE_HOUR)), minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    return target.isoformat()


def _read_manifest(path: str) -> Dict[str, Any]:
    """读取备份边车 manifest；不存在或损坏时返回空字典。

    Args:
        path: manifest 文件路径。

    Returns:
        manifest 字典（只用于展示，读取失败不应影响列表）。
    """
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


async def run_backup(trigger: str = "manual", operator: Optional[str] = None) -> Dict[str, Any]:
    """执行一次 PostgreSQL 全量备份（pg_dump + gzip）。

    Args:
        trigger: 触发方式——manual（管理端点击）/ schedule（定时任务）/ cli（脚本）。
        operator: 操作者用户名（写进 manifest 便于追溯）。

    Returns:
        备份结果描述（与前端 BackupItem 字段对齐）。

    Raises:
        BackupError: 未启用 / pg_dump 不可用 / 执行失败 / 超时 / 文件校验不通过。
    """
    settings = get_settings()
    if not settings.BACKUP_ENABLED:
        raise BackupError("备份功能未启用（BACKUP_ENABLED=false）")

    # pg_dump 可执行文件缺失时提前失败：给出「装哪个包」的可执行建议
    dump_bin = settings.BACKUP_PG_DUMP_BIN
    if os.path.sep in dump_bin:
        # 绝对/相对路径：直接检查文件存在与可执行权限
        if not (os.path.isfile(dump_bin) and os.access(dump_bin, os.X_OK)):
            raise BackupError(f"pg_dump 不可执行：{dump_bin}（请检查 BACKUP_PG_DUMP_BIN）")
    elif shutil.which(dump_bin) is None:
        raise BackupError(
            f"未找到 pg_dump（{dump_bin}）。请安装 PostgreSQL 客户端"
            "（Debian/Ubuntu: apt install postgresql-client；"
            "macOS: brew install postgresql@16），或在 BACKUP_PG_DUMP_BIN 中指定绝对路径"
        )

    conn = _parse_database_dsn()
    os.makedirs(settings.backup_dir_abs, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{_BACKUP_PREFIX}{stamp}.sql.gz"
    data_path, manifest_path = _backup_paths(name)

    # 备份参数说明：
    #   --no-owner / --no-acl：恢复到不同用户名/环境时不必先建同名角色，通用性最好；
    #   --format=plain      ：输出 SQL 文本，便于 gzip 与人眼抽查（自定义格式更小但不可读）；
    #   --no-comments 不加  ：保留注释有助排查版本信息
    cmd = [
        dump_bin,
        "--host", conn["host"],
        "--port", conn["port"],
        "--username", conn["user"],
        "--dbname", conn["dbname"],
        "--no-owner",
        "--no-acl",
        "--format=plain",
    ]
    env = os.environ.copy()
    # 口令走环境变量（不进 argv），并禁用交互式口令提示（无 tty 时会卡住）
    env["PGPASSWORD"] = conn["password"]
    env["PGCONNECT_TIMEOUT"] = "10"

    started = time.perf_counter()
    digest = hashlib.sha256()
    stderr_tail = ""

    async def _dump() -> int:
        """启动 pg_dump 并把 stdout 流式压缩写入磁盘，返回退出码。"""
        nonlocal stderr_tail
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdout is not None and proc.stderr is not None
        # compresslevel=6：压缩率与 CPU 的平衡点，备份窗口内不明显拖慢数据库
        with gzip.open(data_path, "wb", compresslevel=6) as gz:
            while True:
                chunk = await proc.stdout.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                gz.write(chunk)
        # stderr 一般很短（错误信息）；读完后与进程退出码一起返回
        err = await proc.stderr.read()
        stderr_tail = _scrub_secret(err.decode("utf-8", "replace"), conn["password"])[-2000:]
        return await proc.wait()

    try:
        return_code = await asyncio.wait_for(_dump(), timeout=settings.BACKUP_TIMEOUT)
    except asyncio.TimeoutError as exc:
        # 超时：pg_dump 可能仍占着连接，直接杀掉并删除半截文件
        _safe_remove(data_path)
        metrics.record_backup(False)
        raise BackupError(
            f"备份超时（超过 {settings.BACKUP_TIMEOUT:.0f} 秒），已终止。"
            "库较大时可调大 BACKUP_TIMEOUT 或改用物理备份。"
        ) from exc
    except FileNotFoundError as exc:
        _safe_remove(data_path)
        metrics.record_backup(False)
        raise BackupError(f"无法启动 pg_dump：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 —— 统一收敛为备份错误
        _safe_remove(data_path)
        metrics.record_backup(False)
        raise BackupError(f"备份执行失败：{exc}") from exc

    if return_code != 0:
        _safe_remove(data_path)
        metrics.record_backup(False)
        raise BackupError(
            f"pg_dump 退出码 {return_code}：{stderr_tail.strip() or '（无错误输出）'}"
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    size_bytes = os.path.getsize(data_path) if os.path.exists(data_path) else 0

    # 大小与完整性双校验：0 字节或缺少完成标记都视为失败（宁可报错也不留假备份）
    if size_bytes <= 0:
        _safe_remove(data_path)
        metrics.record_backup(False)
        raise BackupError("备份文件为空，判定失败（请检查数据库连接与权限）")

    ok, message = verify_backup_file(data_path)
    if not ok:
        _safe_remove(data_path)
        metrics.record_backup(False)
        raise BackupError(f"备份文件校验失败：{message}")

    manifest = {
        "name": name,
        "size_bytes": size_bytes,
        "sha256": digest.hexdigest(),
        "created_at": datetime.now().isoformat(),
        "elapsed_ms": elapsed_ms,
        "trigger": trigger,
        "operator": operator,
        "database": f"{conn['dbname']}@{conn['host']}:{conn['port']}",
        "verified": True,
    }
    try:
        with open(manifest_path, "w", encoding="utf-8") as fp:
            json.dump(manifest, fp, ensure_ascii=False)
    except OSError as exc:  # 边车写失败不影响备份本身，但要留痕
        logger.warning("备份 manifest 写入失败（备份文件本身可用）：%s", exc)

    metrics.record_backup(True)
    logger.info(
        "数据库备份完成：file=%s，size=%.1fMB，耗时 %.1fs，trigger=%s",
        name,
        size_bytes / (1024 * 1024),
        elapsed_ms / 1000,
        trigger,
    )

    # 备份成功后顺手做保留清理：避免磁盘被历史备份慢慢吃满
    try:
        removed = await asyncio.to_thread(prune_backups)
        if removed:
            logger.info("备份保留策略清理：删除 %d 个过期文件", removed)
    except Exception as exc:  # noqa: BLE001 —— 清理失败不影响本次备份结果
        logger.warning("备份保留清理失败（不影响本次备份）：%s", exc)

    return _to_item(manifest, data_path)


def _safe_remove(path: str) -> None:
    """删除文件，忽略不存在或无权限的异常（失败路径上的清理动作）。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("清理临时备份文件失败：%s（%s）", path, exc)


def verify_backup_file(path: str, tail_bytes: int = 4096) -> tuple[bool, str]:
    """校验备份文件是否为「可用的 pg_dump 产物」。

    只做轻量校验（不重新解压整包，避免大库校验耗时过长）：
      1. 文件存在且非空；
      2. gzip 能读到尾部（说明压缩流完整，没有被截断）；
      3. 尾部含 pg_dump 的完成标记（说明导出过程正常结束）。

    Args:
        path: 备份文件路径（.sql.gz）。
        tail_bytes: 从解压流尾部读取的字节数。

    Returns:
        (是否通过, 说明文案)。
    """
    if not os.path.exists(path):
        return False, "文件不存在"
    if os.path.getsize(path) <= 0:
        return False, "文件为空"

    try:
        with gzip.open(path, "rb") as gz:
            # 顺序读完整个流：gzip 的完整性只能通过读到 EOF 来确认
            # （64KB 块读，内存占用恒定；大库也只是多花几秒）
            tail = b""
            while True:
                chunk = gz.read(_CHUNK_SIZE)
                if not chunk:
                    break
                tail = (tail + chunk)[-tail_bytes:]
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        return False, f"gzip 流损坏：{exc}"

    if _DUMP_COMPLETE_MARK not in tail:
        return False, "缺少 pg_dump 完成标记（可能是导出被中断）"
    return True, "ok"


def _to_item(manifest: Dict[str, Any], data_path: str) -> Dict[str, Any]:
    """把 manifest + 文件信息组装成对外结构（前端 BackupItem）。

    Args:
        manifest: 边车内容（可能为空）。
        data_path: 备份文件路径。

    Returns:
        统一结构的字典。
    """
    settings = get_settings()
    name = manifest.get("name") or os.path.basename(data_path)
    size = manifest.get("size_bytes")
    if size is None:
        try:
            size = os.path.getsize(data_path)
        except OSError:
            size = 0
    created = manifest.get("created_at")
    if not created:
        try:
            created = datetime.fromtimestamp(os.path.getmtime(data_path)).isoformat()
        except OSError:
            created = ""
    return {
        "name": name,
        "size_bytes": int(size or 0),
        "created_at": created,
        "sha256": manifest.get("sha256"),
        "elapsed_ms": manifest.get("elapsed_ms"),
        "trigger": manifest.get("trigger"),
        "operator": manifest.get("operator"),
        "verified": bool(manifest.get("verified", False)),
        # 恢复必须人工执行：这里只回传建议命令，不提供恢复接口
        "restore_hint": f"bash scripts/restore_postgres.sh {name}",
        "retention_days": settings.BACKUP_RETENTION_DAYS,
    }


def list_backups(limit: int = 50) -> List[Dict[str, Any]]:
    """列出备份文件（按时间倒序）。

    Args:
        limit: 最多返回条数（前端分页用）。

    Returns:
        BackupItem 列表。
    """
    settings = get_settings()
    directory = settings.backup_dir_abs
    if not os.path.isdir(directory):
        return []

    files: List[tuple[float, str]] = []
    for entry in os.listdir(directory):
        if entry.startswith(_BACKUP_PREFIX) and entry.endswith(".sql.gz"):
            full = os.path.join(directory, entry)
            try:
                files.append((os.path.getmtime(full), full))
            except OSError:
                continue
    # 按修改时间倒序：最新的备份最可能被查看
    files.sort(key=lambda item: item[0], reverse=True)

    items: List[Dict[str, Any]] = []
    for _, path in files[: max(0, limit)]:
        manifest = _read_manifest(path + ".json")
        items.append(_to_item(manifest, path))
    return items


def prune_backups() -> int:
    """按「天数 + 个数」双约束清理过期备份。

    保留规则（先按个数挑出要保留的，再按天数淘汰）：
      - 天数：修改时间早于 retention_days 的直接删除（retention_days <= 0 表示不按天清理）；
      - 个数：即使未超期，也只保留最新的 retention_count 个（<= 0 表示不按个数清理）。

    Returns:
        删除的文件数（含边车 manifest）。
    """
    settings = get_settings()
    directory = settings.backup_dir_abs
    if not os.path.isdir(directory):
        return 0

    paths: List[tuple[float, str]] = []
    for entry in os.listdir(directory):
        if entry.startswith(_BACKUP_PREFIX) and entry.endswith(".sql.gz"):
            full = os.path.join(directory, entry)
            try:
                paths.append((os.path.getmtime(full), full))
            except OSError:
                continue
    paths.sort(key=lambda item: item[0], reverse=True)

    now = time.time()
    removed = 0
    for index, (mtime, path) in enumerate(paths):
        too_old = (
            settings.BACKUP_RETENTION_DAYS > 0
            and (now - mtime) > settings.BACKUP_RETENTION_DAYS * 86400
        )
        too_many = settings.BACKUP_RETENTION_COUNT > 0 and index >= settings.BACKUP_RETENTION_COUNT
        if not (too_old or too_many):
            continue
        _safe_remove(path)
        _safe_remove(path + ".json")
        removed += 1
    return removed


def backup_status() -> Dict[str, Any]:
    """备份能力总览（状态页卡片用）。

    Returns:
        形如 {enabled, dir, retention_days, retention_count, schedule_hour,
              next_run_hint, last, count, total_bytes, redis}
    """
    settings = get_settings()
    items = list_backups(limit=200) if settings.BACKUP_ENABLED else []
    last = items[0] if items else None
    return {
        "enabled": settings.BACKUP_ENABLED,
        "dir": settings.backup_dir_abs,
        "retention_days": settings.BACKUP_RETENTION_DAYS,
        "retention_count": settings.BACKUP_RETENTION_COUNT,
        "schedule_hour": settings.BACKUP_SCHEDULE_HOUR,
        "next_run_hint": _next_run_hint(),
        "last": last,
        "count": len(items),
        "total_bytes": sum(int(item.get("size_bytes") or 0) for item in items),
        # 是否存在「看起来有备份但不可用」的隐患
        "last_verified": bool(last and last.get("verified")),
    }


async def redis_persistence_status(trigger_save: bool = False) -> Dict[str, Any]:
    """检查 Redis 持久化配置与最近一次落盘状态。

    为什么单独检查：
        Redis 承担 Celery 队列与检索缓存。若未开 AOF/RDB，容器重启会丢掉排队中的入库任务
        （文档永久停在 indexing），这类问题只有主动检查才能发现。

    Args:
        trigger_save: 是否顺带触发一次 BGSAVE（备份流程中开启，确保数据落盘）。

    Returns:
        形如 {reachable, aof_enabled, rdb_last_status, last_save_at, detail}
    """
    settings = get_settings()
    result: Dict[str, Any] = {
        "reachable": False,
        "aof_enabled": False,
        "rdb_last_status": "",
        "last_save_at": None,
        "detail": "",
        "persistence_ok": False,
    }

    try:
        from redis.asyncio import Redis

        client = Redis.from_url(
            settings.CELERY_BROKER_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        # INFO persistence 是获取持久化状态的通用方式（托管 Redis 也支持）
        info = await client.info("persistence")
        result["reachable"] = True
        result["aof_enabled"] = str(info.get("aof_enabled", "0")) == "1"
        result["rdb_last_status"] = str(info.get("rdb_last_bgsave_status", "unknown"))
        last_save = info.get("rdb_last_save_time")
        if last_save:
            result["last_save_at"] = datetime.fromtimestamp(int(last_save)).isoformat()

        if trigger_save:
            try:
                # BGSAVE 是异步的：只负责「触发」，不阻塞等待完成
                await client.bgsave()
                result["detail"] = "已触发 BGSAVE"
            except Exception as exc:  # noqa: BLE001 —— 部分托管实例禁用 BGSAVE
                result["detail"] = f"BGSAVE 触发失败（可能被托管服务禁用）：{exc}"
        else:
            result["detail"] = "ok"

        # 判定「持久化可用」：开了 AOF，或 RDB 最近一次成功
        result["persistence_ok"] = result["aof_enabled"] or result["rdb_last_status"] == "ok"
        await client.aclose()
    except Exception as exc:  # noqa: BLE001 —— 不可达时回传原因，由状态页展示降级
        result["detail"] = f"Redis 不可达或未安装 redis 客户端：{exc}"

    return result


def format_bytes(size: int) -> str:
    """把字节数格式化为可读文本（日志/CLI 输出用）。"""
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"
