# 病毒扫描（P5 功能1：上传安全，对接 ClamAV）
# ---------------------------------------------------------------------
# 对应《需求清单》P5 功能1：后端「/documents/upload 扩展格式，校验 + 大小限制 + 病毒扫描（建议）」。
#
# 为什么用 ClamAV 的 clamd 而不是内置特征库：
#   自建特征库既不可维护也无法应对新样本；ClamAV 是事实标准，clamd 提供
#   INSTREAM 流式扫描接口，可直接扫「刚落盘的临时文件」，无需先入对象存储。
#
# 策略（fail-closed）：
#   - `VIRUS_SCAN_ENABLED=false`（默认）：跳过扫描，仅记录一条 debug 日志；
#   - 开启后 clamd 不可达 / 超时：**拒收**该文件并明确报错。
#     理由：「以为扫过其实没扫」比「明确告知暂时无法接收」危险得多，
#     尤其在上传的是可执行内容伪装成图片/视频时。
#
# 实现说明：
#   clamd 的 INSTREAM 是同步 socket 协议，这里用 asyncio.to_thread 包一层，
#   避免阻塞事件循环；不引入第三方 clamd 客户端库（协议本身很简单）。

from __future__ import annotations

import asyncio
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)

# 单次发送给 clamd 的块大小：1MB 在吞吐与内存占用之间较平衡
_INSTREAM_CHUNK = 1024 * 1024


class VirusScanError(Exception):
    """病毒扫描失败（检出病毒 / 扫描服务不可用）。

    上层（services/documents.py）捕获后转成 HTTP 错误，**不落存储、不落库**。
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ScanResult:
    """扫描结果。

    Attributes:
        scanned: 是否真的执行了扫描（False 表示功能未开启，属正常跳过）。
        clean: 是否干净（scanned=False 时无意义，恒为 True）。
        signature: 检出的病毒名（仅 clean=False 时有值）。
    """

    scanned: bool
    clean: bool
    signature: Optional[str] = None


def _scan_with_clamd(path: Path, host: str, port: int, timeout: float) -> ScanResult:
    """同步执行一次 clamd INSTREAM 扫描（在线程池中调用）。

    Args:
        path: 待扫描文件路径。
        host: clamd 主机。
        port: clamd TCP 端口（默认 3310）。
        timeout: 连接与读写超时（秒）。

    Returns:
        ScanResult（clean=True/False + 病毒名）。

    Raises:
        VirusScanError: 连接失败、协议异常或超时（按 fail-closed 处理）。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            # 1) 握手：zINSTREAM\0（z 前缀表示以 \0 结尾的命令）
            sock.sendall(b"zINSTREAM\0")

            # 2) 连续发送「4 字节大端长度 + 内容」的数据块
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(_INSTREAM_CHUNK)
                    if not chunk:
                        break
                    sock.sendall(struct.pack("!I", len(chunk)))
                    sock.sendall(chunk)
            # 3) 长度为 0 的块表示流结束
            sock.sendall(struct.pack("!I", 0))

            # 4) 读取应答，形如 "stream: OK\0" 或 "stream: Eicar-Test-Signature FOUND\0"
            data = b""
            while not data.endswith(b"\0"):
                part = sock.recv(4096)
                if not part:
                    break
                data += part
    except (OSError, socket.timeout) as exc:
        raise VirusScanError(
            f"病毒扫描服务不可用（{host}:{port}）：{exc}。"
            "如暂不需要扫描，可在 .env 中设置 VIRUS_SCAN_ENABLED=false。",
            status_code=503,
        ) from exc

    reply = data.decode("utf-8", errors="replace").strip().strip("\0")
    if "FOUND" in reply:
        # 形如 "stream: Win.Test.EICAR_HDB-1 FOUND"
        signature = reply.split(":", 1)[-1].replace("FOUND", "").strip()
        logger.warning("检测到恶意文件：%s，特征=%s", path.name, signature)
        return ScanResult(scanned=True, clean=False, signature=signature or "unknown")
    if "OK" in reply:
        return ScanResult(scanned=True, clean=True)
    # 其他应答（如 "stream: ... ERROR"）视为扫描失败，不放行
    raise VirusScanError(f"病毒扫描返回异常结果：{reply or '(空)'}", status_code=503)


async def scan_file(path: Path) -> ScanResult:
    """异步扫描一个已落盘的临时文件。

    Args:
        path: 待扫描文件路径。

    Returns:
        ScanResult：未开启扫描时返回 scanned=False（放行）。

    Raises:
        VirusScanError: 检出病毒（400）或扫描服务不可用（503）。
    """
    settings = get_settings()
    if not settings.VIRUS_SCAN_ENABLED:
        logger.debug("病毒扫描未启用，跳过：%s", path.name)
        return ScanResult(scanned=False, clean=True)

    result = await asyncio.to_thread(
        _scan_with_clamd,
        path,
        settings.VIRUS_SCAN_HOST,
        settings.VIRUS_SCAN_PORT,
        settings.VIRUS_SCAN_TIMEOUT,
    )
    if not result.clean:
        raise VirusScanError(
            f"文件未通过安全扫描（检出 {result.signature}），已拒绝上传",
            status_code=400,
        )
    return result
