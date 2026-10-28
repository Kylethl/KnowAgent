# 多模态文件类型识别与校验（P5 功能1：多模态提交）
# ---------------------------------------------------------------------
# 职责：把「一个上传文件的扩展名 + 开头字节」判定为可信的文档类型，
#       为上传校验（大小分级 / 魔数校验 / MIME 推断）提供唯一依据。
#
# 对应《需求清单》P5 功能1：
#   - 后端：`/documents/upload` 扩展格式，校验 + 大小限制 + 病毒扫描（建议）
#   - 数据存储：`documents.type` 扩展 image/audio/video
#
# 设计要点：
#   1. **扩展名只是第一道门**：把 evil.exe 改名成 .png 是最省事的绕过方式，
#      因此再用「文件头魔数」确认容器格式与扩展名一致（见 verify_magic）。
#   2. **无法判定时放行**：部分合法容器（如某些老式 .avi 变体）没有稳定的魔数特征，
#      一律拒收会误杀真实素材；此时返回 None（未知），由后续解析阶段兜底。
#   3. 大小上限按类型分级：图片/音频/视频的体积量级差一个数量级，
#      共用 20MB 会「图片浪费、视频不够」（见 config.max_upload_size_for）。
#   4. 本模块**不依赖**第三方库：魔数判定用纯字节比较，避免为一次校验引入重型依赖。

from __future__ import annotations

from typing import Optional

# 文档类型 → 是否为多模态（P5 新增的三类）
MEDIA_FILE_TYPES = ("image", "audio", "video")

# 扩展名 → 文档类型（与 documents.file_type 的 CHECK 约束取值一致）
# 该映射是「扩展名 → 类型」的唯一来源，接口层、服务层、前端文案都以此为准。
EXTENSION_TO_TYPE: dict[str, str] = {
    # ---- 文档（P1）----
    "doc": "word",
    "docx": "word",
    "md": "markdown",
    # ---- 图片（P5）----
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "webp": "image",
    "bmp": "image",
    "gif": "image",
    # ---- 音频（P5）----
    "mp3": "audio",
    "wav": "audio",
    "m4a": "audio",
    "aac": "audio",
    "flac": "audio",
    "ogg": "audio",
    # ---- 视频（P5）----
    "mp4": "video",
    "mov": "video",
    "m4v": "video",
    "avi": "video",
    "mkv": "video",
    "webm": "video",
}

# 扩展名 → MIME 类型（上传方未提供 Content-Type 时的兜底，也用于媒体预览响应头）
EXTENSION_TO_MIME: dict[str, str] = {
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "gif": "image/gif",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "m4v": "video/x-m4v",
    "avi": "video/x-msvideo",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
}

# 扩展名 → 可接受的容器格式（魔数判定结果）。
# 允许多个候选是因为同一扩展名常对应多个容器（如 .mov 与 .mp4 都是 ISO-BMFF）。
_EXT_TO_CONTAINERS: dict[str, set[str]] = {
    "jpg": {"jpeg"},
    "jpeg": {"jpeg"},
    "png": {"png"},
    "gif": {"gif"},
    "webp": {"webp"},
    "bmp": {"bmp"},
    "mp3": {"mp3"},
    "wav": {"wav"},
    "m4a": {"isobmff"},
    "aac": {"aac"},
    "flac": {"flac"},
    "ogg": {"ogg"},
    "mp4": {"isobmff"},
    "mov": {"isobmff"},
    "m4v": {"isobmff"},
    "avi": {"avi"},
    "mkv": {"matroska"},
    "webm": {"matroska"},
}

# 判定魔数时读取的头部字节数：ISO-BMFF 的 ftyp 位于偏移 4~8，
# WebP 的 WEBP 标记位于偏移 8，取 32 字节足以覆盖本项目全部判定规则。
MAGIC_HEAD_BYTES = 32


def is_media_type(file_type: str) -> bool:
    """判断某个文档类型是否属于多模态（图片 / 音频 / 视频）。

    Args:
        file_type: 文档类型（word / markdown / image / audio / video）。

    Returns:
        True 表示需要 OCR/ASR 解析后入库。
    """
    return file_type in MEDIA_FILE_TYPES


def resolve_file_type(ext: str) -> Optional[str]:
    """按扩展名推导文档类型。

    Args:
        ext: 不含点的小写扩展名（如 "png"）。

    Returns:
        文档类型；扩展名不在白名单内时返回 None。
    """
    return EXTENSION_TO_TYPE.get((ext or "").lower())


def guess_mime(ext: str, fallback: Optional[str] = None) -> str:
    """推断 MIME 类型。

    优先用映射表（它比浏览器给的 Content-Type 更可信——浏览器对 .md / .m4a
    常给出 application/octet-stream），无映射时回退到调用方提供的值。

    Args:
        ext: 不含点的小写扩展名。
        fallback: 兜底 MIME（通常是上传请求里的 Content-Type）。

    Returns:
        MIME 字符串；两者都缺失时返回 application/octet-stream。
    """
    return EXTENSION_TO_MIME.get((ext or "").lower()) or (fallback or "application/octet-stream")


def detect_container(head: bytes) -> Optional[str]:
    """按文件头魔数判定容器格式。

    Args:
        head: 文件开头的若干字节（建议至少 MAGIC_HEAD_BYTES 字节）。

    Returns:
        容器标识（jpeg / png / gif / webp / bmp / mp3 / wav / isobmff / aac /
        flac / ogg / avi / matroska）；无法判定时返回 None。
    """
    if not head:
        return None

    # ---- 图片 ----
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head.startswith(b"BM"):
        return "bmp"
    # WebP：RIFF....WEBP（大小字段在偏移 4~8，故需比较 8 字节后的标记）
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"

    # ---- 音频 ----
    if head.startswith(b"ID3"):
        return "mp3"
    # MPEG 音频帧同步字：11 位全 1（0xFFE0 掩码）
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # AAC(ADTS) 与 MP3 都以此开头，靠 layer 位区分：MPEG-1/2 Layer III → MP3
        # ADTS 的 layer 位恒为 00，故 (head[1] & 0x06) == 0 时判为 AAC
        return "aac" if (head[1] & 0x06) == 0 else "mp3"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"OggS"):
        return "ogg"

    # ---- 视频 / 音视频容器 ----
    # ISO-BMFF（mp4 / mov / m4a / m4v）：偏移 4 起为 "ftyp"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "isobmff"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "avi"
    # Matroska / WebM：EBML 头 0x1A45DFA3
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"

    # ZIP 容器（docx/xlsx 的本质）：单独识别，用于判定「改名的压缩包/可执行包」
    if head.startswith(b"PK\x03\x04"):
        return "zip"

    return None


def verify_magic(head: bytes, ext: str) -> Optional[bool]:
    """校验文件头魔数与扩展名是否一致。

    Args:
        head: 文件开头字节（建议 ≥ MAGIC_HEAD_BYTES）。
        ext: 不含点的小写扩展名。

    Returns:
        True  —— 魔数与扩展名匹配；
        False —— 明显不匹配（如 .png 实际是 zip/exe），应拒收；
        None  —— 无法判定（未知容器或无候选规则），按放行处理。
    """
    expected = _EXT_TO_CONTAINERS.get((ext or "").lower())
    if not expected:
        # 文档类（docx/doc/md）不走魔数校验：docx 本质是 zip，判定意义有限
        return None

    actual = detect_container(head)
    if actual is None:
        # 头部不符合任何一种已知特征：可能是残缺文件或冷门变体，
        # 此时不武断拒收（否则会误杀合法素材），交给解析阶段暴露问题。
        return None
    # ZIP 容器伪装成图片/音视频是最常见的「改名绕过」形态
    #（把压缩包或含宏的 office 文档改成 .png），明确判为不匹配。
    if actual == "zip":
        return False
    return actual in expected
