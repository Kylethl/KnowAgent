# 多模态内容提取（P5 功能2：图片 OCR / 音频视频 ASR）
# ---------------------------------------------------------------------
# 对应《需求清单》P5 功能2：
#   - 后端：Celery 任务——图片 OCR 或视觉理解；音频/视频 ASR 转写
#   - AI Agent：对图片 / 转写文本生成向量，切片入库 pgvector
#
# 落地选型（与项目实际可用模型对齐）：
#   - OCR：`qwen3.5-ocr`（百炼 compatible-mode 网关，OpenAI 兼容 chat/completions，
#          输入 image_url data URI，输出即纯文本）。实测比 qwen-vl-ocr 合适——
#          后者只返回坐标框，拿不到文字。
#   - ASR：`qwen3.8-omni-flash`（同网关，OpenAI 风格 input_audio + data URI）。
#          网关上的 fun-asr / qwen-audio-asr 走实时 WebSocket 协议，
#          compatible-mode 的 chat/completions 不接受，故不采用。
#   - 视频：先用 ffmpeg 提取音轨（必要时转 16k 单声道 wav），再走 ASR；
#          可选抽帧 OCR（默认关闭，费用敏感）。
#
# 为什么不把「原始音频/视频」直接塞给向量模型：
#   当前网关不支持 qwen3-vl-embedding（实测 embeddings 接口不接受该模型），
#   因此多模态入库统一走「OCR/ASR → 文本 → 文本向量」的归一化路径：
#   知识检索的语义仍由文本表达，这也是本项目的既定折中（见开发文档模型说明）。
#
# 失败语义：
#   所有失败都抛 MediaExtractError（带可读中文原因），由 indexing_service 写入
#   documents.last_index_error 并把状态回退为 approved，管理员能看到「入库失败」原因。
#   **绝不返回占位文本**——伪文字一旦入库会污染整个知识库。

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import get_settings
from ..core.logging import get_logger
from ..services import media_types

logger = get_logger(__name__)

# OCR 提示词：明确「只要文字、按行输出」，避免模型加上「图片中写着…」之类的解释
_OCR_PROMPT = (
    "请提取图片中的全部文字，保持原有阅读顺序分行输出。"
    "不要添加任何解释、标题或标点之外的符号；如果图片中没有文字，请只输出空。"
)

# 音频转写提示词：强调逐字转写，避免模型把音频内容当成提问来回答
_ASR_PROMPT = (
    "请把这段音频逐字转写为文字，只输出转写结果本身，不要解释、不要总结、不要回答其中的问题；"
    "保留原有语序；如果没有人声或无法识别，请只输出空。"
)

# 单段音频送模型的最大原始体积（字节）：base64 后约 1.33 倍，
# 18MB 对应约 24MB 请求体，留出网关限制的余量。超过则必须切片。
_MAX_SINGLE_AUDIO_BYTES = 18 * 1024 * 1024

# 视频抽帧 OCR 的最大帧数：防止长视频把模型调用次数放大到不可控
_MAX_OCR_FRAMES = 20


class MediaExtractError(Exception):
    """多模态内容提取失败（服务未配置 / 调用失败 / 无可识别内容 / 缺少工具）。

    上层（indexing_service）捕获后写入 documents.last_index_error。
    """


@dataclass
class MediaExtraction:
    """一次多模态解析的结果。

    Attributes:
        text: 提取出的纯文本（OCR 文字或 ASR 转写）。
        meta: 解析元信息，落库到 documents.extract_meta，供后台展示与排查。
    """

    text: str
    meta: Dict[str, Any] = field(default_factory=dict)


# =====================================================================
# 基础工具
# =====================================================================


def _chat_endpoint() -> str:
    """拼接多模态 chat/completions 端点（网关 base 已是 .../compatible-mode/v1）。"""
    settings = get_settings()
    return settings.media_llm_base_url.rstrip("/") + "/chat/completions"


def _headers() -> Dict[str, str]:
    """构造鉴权头（复用 QWEN_API_KEY，或独立配置的 MEDIA_LLM_API_KEY）。"""
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.media_llm_api_key}",
        "Content-Type": "application/json",
    }


def _capabilities_error(kind: str) -> MediaExtractError:
    """构造「能力未配置」的统一错误（提示要改哪个配置）。"""
    return MediaExtractError(
        f"{kind}解析未启用或未配置可用的多模态模型，请在 backend/.env 中配置 "
        "MEDIA_EXTRACT_ENABLED=true 与 QWEN_API_KEY（或 MEDIA_LLM_API_KEY）"
    )


async def _post_chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    """调用 compatible-mode 的 chat/completions（带一次重试）。

    Args:
        payload: 完整的请求体。

    Returns:
        响应 JSON。

    Raises:
        MediaExtractError: 网络异常 / 非 200 / 响应结构异常（重试后仍失败）。
    """
    settings = get_settings()
    url = _chat_endpoint()
    last_error: Optional[str] = None

    # 两次尝试：多模态调用偶发超时较常见，重试一次能显著降低「入库失败」概率
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=settings.MEDIA_EXTRACT_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=_headers())
            if resp.status_code == 200:
                return resp.json()
            # 4xx 多为模型名/参数/额度问题，重试无意义
            body = resp.text[:300]
            if 400 <= resp.status_code < 500:
                raise MediaExtractError(f"多模态模型调用被拒绝（HTTP {resp.status_code}）：{body}")
            last_error = f"HTTP {resp.status_code}：{body}"
        except MediaExtractError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt == 0:
            await asyncio.sleep(1.0)

    raise MediaExtractError(f"多模态模型调用失败（已重试）：{last_error}")


def _extract_message_text(data: Dict[str, Any]) -> str:
    """从 chat/completions 响应中取出正文文本。

    兼容两种返回：`message.content` 与 OCR 模型附带的 `ocr_result.processed_text`。

    Args:
        data: 响应 JSON。

    Returns:
        模型输出的文本（可能为空字符串）。
    """
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MediaExtractError(f"多模态响应结构异常：{str(data)[:200]}") from exc

    # OCR 专用字段优先：它对「多行文字」的顺序处理比 content 更规整
    ocr = message.get("ocr_result")
    if isinstance(ocr, dict) and isinstance(ocr.get("processed_text"), str):
        return ocr["processed_text"].strip()
    return str(message.get("content") or "").strip()


def _clean_ocr_text(text: str) -> str:
    """清洗 OCR 文本：去掉 qwen-vl-ocr 风格的坐标前缀与多余空行。

    qwen3.5-ocr 有时返回 `<x1,y1,x2,y2,angle,正文` 形式的行，
    这类坐标对知识检索毫无价值，还可能被当成正文切进向量，故整体剥离。

    Args:
        text: 模型原始输出。

    Returns:
        清洗后的文本。
    """
    lines: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        # 剥离形如 "<248,230,119,409,90," 的坐标前缀
        line = re.sub(r"^<[\d,\s]+,\s*", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


# =====================================================================
# 图片 OCR
# =====================================================================


def _prepare_image_bytes(content: bytes) -> Tuple[bytes, str]:
    """把图片压缩到适合送模型的尺寸（Pillow 不可用时原样返回）。

    为什么需要压缩：手机原图 4000px、5MB 以上，base64 后请求体过大且 OCR 不需要那么高分辨率；
    压到长边 1600px + JPEG q85 通常只有 200~500KB，文字依然清晰。

    Args:
        content: 原始图片字节。

    Returns:
        (处理后的字节, MIME)。Pillow 缺失或处理失败时返回 (原字节, 按魔数推断的 MIME)。
    """
    settings = get_settings()
    try:
        from io import BytesIO

        from PIL import Image  # 懒导入：未装 Pillow 不影响其他类型入库

        with Image.open(BytesIO(content)) as img:
            # 统一转 RGB：PNG 的透明通道转 JPEG 会报错，先铺白底
            if img.mode not in ("RGB", "L"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                background.paste(rgba, mask=rgba.split()[-1])
                img = background
            elif img.mode == "L":
                img = img.convert("RGB")

            # 等比缩放：只缩不放
            longest = max(img.size)
            if longest > settings.MEDIA_IMAGE_MAX_EDGE:
                ratio = settings.MEDIA_IMAGE_MAX_EDGE / float(longest)
                img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))

            out = BytesIO()
            img.save(out, format="JPEG", quality=settings.MEDIA_IMAGE_JPEG_QUALITY, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception as exc:  # noqa: BLE001 —— 压缩是优化项，失败不应阻断 OCR
        logger.warning("图片预处理失败（将原样送模型）：%s", exc)
        container = media_types.detect_container(content[:32]) or "png"
        mime = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}.get(
            container, "image/png"
        )
        return content, mime


async def ocr_image(content: bytes, filename: str = "") -> MediaExtraction:
    """对图片做 OCR，返回提取到的文字。

    Args:
        content: 图片字节。
        filename: 原始文件名（仅日志用）。

    Returns:
        MediaExtraction（text 为文字，meta 含模型与耗时）。

    Raises:
        MediaExtractError: 能力未配置、调用失败或未识别到文字。
    """
    settings = get_settings()
    if not settings.is_media_ocr_ready:
        raise _capabilities_error("图片 OCR")

    prepared, mime = _prepare_image_bytes(content)
    data_uri = f"data:{mime};base64,{base64.b64encode(prepared).decode()}"

    payload = {
        "model": settings.MEDIA_OCR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
    }

    started = time.perf_counter()
    data = await _post_chat(payload)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    text = _clean_ocr_text(_extract_message_text(data))
    if not text:
        raise MediaExtractError("图片中未识别到文字内容（可能是纯图片/照片），无法生成可检索的知识")

    logger.info(
        "图片 OCR 完成：file=%s，字符数=%d，耗时=%dms，模型=%s",
        filename, len(text), elapsed_ms, settings.MEDIA_OCR_MODEL,
    )
    return MediaExtraction(
        text=text,
        meta={
            "kind": "image",
            "provider": "qwen-compatible",
            "model": settings.MEDIA_OCR_MODEL,
            "chars": len(text),
            "elapsed_ms": elapsed_ms,
            "compressed_bytes": len(prepared),
        },
    )


# =====================================================================
# 音频 / 视频 ASR
# =====================================================================


def _resolve_ffmpeg() -> Optional[str]:
    """定位可用的 ffmpeg 可执行文件。

    查找顺序：配置的 MEDIA_FFMPEG_BIN → PATH 中的 ffmpeg → imageio-ffmpeg 自带的静态二进制。
    最后一项让「本机没装 ffmpeg」的开发环境也能处理音视频，
    不必为了一个转码去装系统级依赖。

    Returns:
        ffmpeg 路径；都不可用时返回 None。
    """
    settings = get_settings()
    configured = (settings.MEDIA_FFMPEG_BIN or "ffmpeg").strip()
    found = shutil.which(configured)
    if found:
        return found
    try:
        import imageio_ffmpeg  # 懒导入：未安装时走「无 ffmpeg」分支

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:  # noqa: BLE001 —— 缺依赖/二进制损坏都按「不可用」处理
        pass
    return None


async def _run_ffmpeg(args: List[str], timeout: float = 300.0) -> Tuple[int, str]:
    """执行一次 ffmpeg 命令（异步，捕获 stderr 用于解析时长）。

    Args:
        args: 完整命令行参数（含 ffmpeg 路径）。
        timeout: 超时（秒）。

    Returns:
        (返回码, stderr 文本)。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, (stderr or b"").decode("utf-8", errors="replace")
    except asyncio.TimeoutError as exc:
        raise MediaExtractError(f"ffmpeg 处理超时（>{timeout:.0f}s），音视频可能过长") from exc
    except FileNotFoundError as exc:
        raise MediaExtractError("未找到 ffmpeg，无法处理音视频") from exc


def _parse_duration(stderr: str) -> Optional[float]:
    """从 ffmpeg 的 stderr 中解析时长（秒）。

    ffmpeg 会打印形如 `Duration: 00:01:23.45, start: ...` 的元信息行；
    用 ffmpeg 而非 ffprobe 解析，是因为 imageio-ffmpeg 只附带 ffmpeg。

    Args:
        stderr: ffmpeg 的错误输出。

    Returns:
        时长（秒）；解析失败返回 None。
    """
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


async def _probe_duration(ffmpeg: str, src: Path) -> Optional[float]:
    """读取媒体时长（秒）。失败返回 None（不阻断后续转写）。"""
    code, stderr = await _run_ffmpeg([ffmpeg, "-hide_banner", "-i", str(src)], timeout=60)
    # 仅探测而不输出时 ffmpeg 会以非 0 退出，这是正常的
    _ = code
    return _parse_duration(stderr)


async def _to_wav_16k(ffmpeg: str, src: Path, dst: Path) -> None:
    """把任意音频转成 16kHz 单声道 wav（ASR 的标准输入，体积也更小）。

    Args:
        ffmpeg: ffmpeg 路径。
        src: 源文件。
        dst: 目标 wav 路径。

    Raises:
        MediaExtractError: 转码失败（文件损坏/无音轨等）。
    """
    code, stderr = await _run_ffmpeg(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(dst)]
    )
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise MediaExtractError(f"音频转码失败：{stderr[-300:] or '未知原因'}")


async def _split_wav(
    ffmpeg: str, src: Path, out_dir: Path, segment_seconds: int
) -> List[Path]:
    """把 wav 按固定时长切片（ASR 无法一次性处理超长音频）。

    Args:
        ffmpeg: ffmpeg 路径。
        src: 源 wav。
        out_dir: 输出目录。
        segment_seconds: 每段秒数。

    Returns:
        按序号排好的分片路径列表。
    """
    pattern = out_dir / "seg_%04d.wav"
    code, stderr = await _run_ffmpeg(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-f", "segment", "-segment_time", str(segment_seconds),
         "-reset_timestamps", "1", str(pattern)]
    )
    if code != 0:
        raise MediaExtractError(f"音频切片失败：{stderr[-300:] or '未知原因'}")
    return sorted(out_dir.glob("seg_*.wav"))


async def _transcribe_wav_bytes(content: bytes) -> str:
    """把一段 wav 字节送 ASR 模型转写，返回文本。

    Args:
        content: wav 文件字节。

    Returns:
        转写文本（可能为空字符串）。

    Raises:
        MediaExtractError: 调用失败。
    """
    settings = get_settings()
    payload = {
        "model": settings.MEDIA_ASR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _ASR_PROMPT},
                    {
                        "type": "input_audio",
                        # 网关要求 data URI（只给裸 base64 会报 "URL does not appear to be valid"）
                        "input_audio": {
                            "data": f"data:audio/wav;base64,{base64.b64encode(content).decode()}",
                            "format": "wav",
                        },
                    },
                ],
            }
        ],
    }
    data = await _post_chat(payload)
    return _extract_message_text(data).strip()


async def transcribe_media(
    path: Path, file_type: str, filename: str = ""
) -> MediaExtraction:
    """转写音频 / 视频中的语音内容。

    流程：探测时长 → （必要时）提取音轨 → 转 16k 单声道 wav → 按段切片 → 逐段 ASR → 拼接。
    视频额外支持抽帧 OCR（MEDIA_VIDEO_OCR_ENABLED=true 时）。

    Args:
        path: 已落盘的媒体文件路径。
        file_type: 'audio' 或 'video'。
        filename: 原始文件名（日志用）。

    Returns:
        MediaExtraction（text 为转写文本，meta 含时长/段数/抽取方式）。

    Raises:
        MediaExtractError: 能力未配置、缺少 ffmpeg、超长、转写失败或无人声。
    """
    settings = get_settings()
    if not settings.is_media_asr_ready:
        raise _capabilities_error("音频/视频")

    ffmpeg = _resolve_ffmpeg()
    duration = await _probe_duration(ffmpeg, path) if ffmpeg else None
    started = time.perf_counter()

    # 无 ffmpeg 的降级路径：
    #   - 音频：直接把原文件按 data URI 送出（wav/mp3/m4a 均可用，但不转码、不切片）；
    #   - 视频：必须解封装出音轨，没有 ffmpeg 就只能明确失败。
    if not ffmpeg:
        if file_type == "video":
            raise MediaExtractError(
                "服务器未安装 ffmpeg，无法从视频中提取音轨。"
                "请安装 ffmpeg（Docker 镜像已内置）或改传音频文件。"
            )
        raw = path.read_bytes()
        if len(raw) > _MAX_SINGLE_AUDIO_BYTES:
            raise MediaExtractError(
                "音频体积过大且服务器未安装 ffmpeg，无法切片转写。"
                "请安装 ffmpeg 或上传更短的音频。"
            )
        text = await _transcribe_wav_bytes(raw)  # 假设已是可解码音频，网关自行识别
        if not text:
            raise MediaExtractError("音频中未识别到有效语音内容，无法生成可检索的知识")
        return MediaExtraction(
            text=text,
            meta={
                "kind": file_type,
                "provider": "qwen-compatible",
                "model": settings.MEDIA_ASR_MODEL,
                "chars": len(text),
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "extractor": "none",
                "segments": 1,
            },
        )

    with tempfile.TemporaryDirectory(prefix="knowagent_asr_") as tmpdir:
        tmp = Path(tmpdir)
        wav_path = tmp / "audio.wav"

        # 1) 视频先抽音轨（-vn 丢弃视频流）；音频直接转码
        await _to_wav_16k(ffmpeg, path, wav_path)

        if duration is None:
            duration = await _probe_duration(ffmpeg, wav_path)

        # 2) 计算分片数：按时长切片；未探测到时长时按体积粗判是否需要切
        need_split = False
        if duration:
            need_split = duration > settings.MEDIA_ASR_SEGMENT_SECONDS
        else:
            need_split = wav_path.stat().st_size > _MAX_SINGLE_AUDIO_BYTES

        segments: List[Path] = [wav_path]
        if need_split:
            segments = await _split_wav(ffmpeg, wav_path, tmp, settings.MEDIA_ASR_SEGMENT_SECONDS)
            if not segments:
                raise MediaExtractError("音频切片结果为空，无法转写")

        # 3) 段数上限保护：超长音频的费用/时延不可控，明确拒绝而不是硬跑
        if len(segments) > settings.MEDIA_ASR_MAX_SEGMENTS:
            limit_min = settings.MEDIA_ASR_SEGMENT_SECONDS * settings.MEDIA_ASR_MAX_SEGMENTS / 60
            raise MediaExtractError(
                f"媒体时长过长（约 {duration / 60:.0f} 分钟），"
                f"超过单文件转写上限 {limit_min:.0f} 分钟，请拆分后上传"
            )

        parts: List[str] = []
        for idx, seg in enumerate(segments, start=1):
            raw = seg.read_bytes()
            # 单段仍过大（如码率很高）：直接报错，避免请求体被网关拒绝
            if len(raw) > _MAX_SINGLE_AUDIO_BYTES:
                raise MediaExtractError(
                    f"音频第 {idx} 段体积过大（{len(raw) / 1024 / 1024:.1f}MB），无法送模型转写"
                )
            text = await _transcribe_wav_bytes(raw)
            if text:
                parts.append(text)
            logger.info(
                "音频转写进度：%s %d/%d（本段字符数=%d）", filename, idx, len(segments), len(text)
            )

        transcript = "\n".join(parts).strip()
        if not transcript:
            raise MediaExtractError("音频中未识别到有效语音内容，无法生成可检索的知识")

        # 4) 视频可选抽帧 OCR：把画面里的文字（如 PPT、板书）也纳入知识
        frame_text = ""
        frames = 0
        if file_type == "video" and settings.MEDIA_VIDEO_OCR_ENABLED and settings.is_media_ocr_ready:
            frame_text, frames = await _ocr_video_frames(ffmpeg, path, tmp)

    if frame_text:
        transcript = f"{transcript}\n\n【画面文字】\n{frame_text}"

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "媒体转写完成：file=%s，时长=%s，段数=%d，字符数=%d，耗时=%dms，模型=%s",
        filename,
        f"{duration:.1f}s" if duration else "未知",
        len(segments),
        len(transcript),
        elapsed_ms,
        settings.MEDIA_ASR_MODEL,
    )
    return MediaExtraction(
        text=transcript,
        meta={
            "kind": file_type,
            "provider": "qwen-compatible",
            "model": settings.MEDIA_ASR_MODEL,
            "chars": len(transcript),
            "elapsed_ms": elapsed_ms,
            "extractor": "ffmpeg",
            "segments": len(segments),
            "duration_seconds": round(duration, 2) if duration else None,
            "ocr_frames": frames,
        },
    )


async def _ocr_video_frames(ffmpeg: str, src: Path, tmp: Path) -> Tuple[str, int]:
    """按间隔抽帧并逐帧 OCR（仅 PPT/板书类视频有意义）。

    Args:
        ffmpeg: ffmpeg 路径。
        src: 视频文件。
        tmp: 临时目录。

    Returns:
        (合并后的画面文字, 实际处理帧数)。失败时返回 ("", 0)，不影响音轨转写结果。
    """
    settings = get_settings()
    frame_dir = tmp / "frames"
    frame_dir.mkdir(exist_ok=True)
    # fps=1/interval：每 interval 秒抽一帧；-frames:v 限制总帧数
    code, stderr = await _run_ffmpeg(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vf", f"fps=1/{max(1, settings.MEDIA_VIDEO_OCR_INTERVAL)}",
         "-frames:v", str(_MAX_OCR_FRAMES), str(frame_dir / "f_%04d.jpg")]
    )
    if code != 0:
        logger.warning("视频抽帧失败（跳过画面 OCR）：%s", stderr[-200:])
        return "", 0

    texts: List[str] = []
    frames = sorted(frame_dir.glob("f_*.jpg"))
    for frame in frames:
        try:
            result = await ocr_image(frame.read_bytes(), frame.name)
            texts.append(result.text)
        except MediaExtractError as exc:
            # 单帧失败（如纯画面无文字）不应让整个入库失败
            logger.debug("抽帧 OCR 跳过：%s（%s）", frame.name, exc)
    return "\n".join(texts).strip(), len(frames)


# =====================================================================
# 统一入口
# =====================================================================


async def extract_media(
    path: Path, file_type: str, filename: str = ""
) -> MediaExtraction:
    """按文档类型提取多模态文本（OCR / ASR 的统一入口）。

    Args:
        path: 已落盘的媒体文件路径。
        file_type: image / audio / video。
        filename: 原始文件名（日志与元信息用）。

    Returns:
        MediaExtraction。

    Raises:
        MediaExtractError: 类型不支持或解析失败。
    """
    if file_type == "image":
        # 图片体积受 10MB 上限约束，直接读入内存即可
        return await ocr_image(path.read_bytes(), filename)
    if file_type in ("audio", "video"):
        return await transcribe_media(path, file_type, filename)
    raise MediaExtractError(f"暂不支持解析该类型：{file_type}")


async def save_object_to_temp(storage, key: str, suffix: str = "") -> Path:
    """把对象存储中的文件流式落到临时文件（避免整段读进内存）。

    为什么需要：音视频可达 200MB，`storage.get_object()` 会一次性读入内存；
    入库流程改为「流式落盘 → 按文件路径处理」，内存占用与文件大小解耦。

    Args:
        storage: 存储后端实例。
        key: 对象相对键。
        suffix: 临时文件后缀（保留扩展名便于 ffmpeg 识别容器）。

    Returns:
        临时文件路径（调用方负责删除）。

    Raises:
        MediaExtractError: 读取失败。
    """
    tmp = tempfile.NamedTemporaryFile(
        prefix="knowagent_media_", suffix=suffix or Path(key).suffix, delete=False
    )
    tmp_path = Path(tmp.name)
    try:
        with open(tmp_path, "wb") as out:
            async for chunk in storage.iter_object(key):
                out.write(chunk)
    except Exception as exc:  # noqa: BLE001 —— 存储异常统一转成可读错误
        tmp_path.unlink(missing_ok=True)
        raise MediaExtractError(f"读取原始文件失败：{exc}") from exc
    finally:
        tmp.close()
    return tmp_path


def capabilities() -> Dict[str, Any]:
    """返回多模态解析能力快照（供 /health 与后台提示）。

    Returns:
        形如 {"enabled": bool, "ocr": bool, "asr": bool, "ocr_model": str,
              "asr_model": str, "ffmpeg": str|None} 的字典。
    """
    settings = get_settings()
    return {
        "enabled": settings.MEDIA_EXTRACT_ENABLED,
        "ocr": settings.is_media_ocr_ready,
        "asr": settings.is_media_asr_ready,
        "ocr_model": settings.MEDIA_OCR_MODEL,
        "asr_model": settings.MEDIA_ASR_MODEL,
        "ffmpeg": _resolve_ffmpeg(),
    }
