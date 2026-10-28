# P5 功能2「多模态入库（OCR / ASR）」单元测试
# ---------------------------------------------------------------------
# 覆盖（全部不触网、不依赖真实模型）：
#   1. 响应解析与文本清洗（_extract_message_text / _clean_ocr_text）
#   2. 图片 OCR：正常取文本、无文字报错、能力未配置报错
#   3. 音频/视频转写：无 ffmpeg 时的降级语义、有 ffmpeg 时的分段拼接
#   4. 提取文本 → ParsedBlock 的归一化（parse_extracted_text）
#   5. 统一入口 extract_media 的类型分发
#   6. 能力快照 capabilities() 的字段完整性
#
# 说明：真实模型与 ffmpeg 的端到端行为由开发报告中的「真实环境验证」覆盖，
#      这里只锁定可确定性验证的逻辑与错误语义。

import asyncio
from pathlib import Path

import pytest

from app.config import get_settings
from app.services import media_extract as me
from app.services.parser import DocumentParseError, parse_extracted_text

# 真实 wav 文件头（仅用于让「读文件」这一步成立，模型调用被打桩）
_WAV_BYTES = b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 64


# =====================================================================
# 1. 响应解析与文本清洗
# =====================================================================
def test_extract_message_text_prefers_ocr_result():
    data = {
        "choices": [
            {
                "message": {
                    "content": "带坐标的原始输出",
                    "ocr_result": {"processed_text": "干净的文本"},
                }
            }
        ]
    }
    assert me._extract_message_text(data) == "干净的文本"


def test_extract_message_text_falls_back_to_content():
    data = {"choices": [{"message": {"content": "  转写结果  "}}]}
    assert me._extract_message_text(data) == "转写结果"


def test_extract_message_text_bad_structure():
    with pytest.raises(me.MediaExtractError):
        me._extract_message_text({"choices": []})


def test_clean_ocr_text_strips_coordinates():
    # qwen-vl-ocr 风格的行内坐标前缀必须剥掉，否则会被当成正文入向量
    raw = "<248,230,119,409,90,知识库多模态测试\n<290,468,107,488,90,第二行"
    assert me._clean_ocr_text(raw) == "知识库多模态测试\n第二行"


def test_clean_ocr_text_drops_blank_lines():
    assert me._clean_ocr_text("\n\n第一行\n\n   \n第二行\n") == "第一行\n第二行"


# =====================================================================
# 2. 图片 OCR
# =====================================================================
def test_ocr_image_returns_text(monkeypatch):
    async def fake_post(payload):
        # 校验请求体携带的是 image_url data URI 与配置的模型
        assert payload["model"] == get_settings().MEDIA_OCR_MODEL
        content = payload["messages"][0]["content"]
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/")
        return {"choices": [{"message": {"content": "识别到的文字"}}]}

    monkeypatch.setattr(me, "_post_chat", fake_post)
    result = asyncio.run(me.ocr_image(b"\x89PNG\r\n\x1a\n" + b"0" * 32, "a.png"))
    assert result.text == "识别到的文字"
    assert result.meta["kind"] == "image"
    assert result.meta["chars"] == len("识别到的文字")


def test_ocr_image_raises_when_no_text(monkeypatch):
    async def fake_post(payload):
        return {"choices": [{"message": {"content": "   "}}]}

    monkeypatch.setattr(me, "_post_chat", fake_post)
    with pytest.raises(me.MediaExtractError) as exc:
        asyncio.run(me.ocr_image(b"\x89PNG\r\n\x1a\n" + b"0" * 32))
    # 明确失败而不是返回空文本：空文本入库会产生 0 切片却显示「已入库」
    assert "未识别到文字" in str(exc.value)


def test_ocr_image_requires_configuration(monkeypatch):
    monkeypatch.setattr(get_settings(), "MEDIA_EXTRACT_ENABLED", False)
    with pytest.raises(me.MediaExtractError) as exc:
        asyncio.run(me.ocr_image(b"\x89PNG\r\n\x1a\n" + b"0" * 32))
    assert "未启用" in str(exc.value)


# =====================================================================
# 3. 音频 / 视频转写
# =====================================================================
def test_transcribe_audio_without_ffmpeg(monkeypatch, tmp_path):
    # 无 ffmpeg 时：音频直接原样送模型（不转码、不切片）
    monkeypatch.setattr(me, "_resolve_ffmpeg", lambda: None)

    async def fake_post(payload):
        content = payload["messages"][0]["content"]
        assert content[1]["type"] == "input_audio"
        assert content[1]["input_audio"]["data"].startswith("data:audio/wav;base64,")
        return {"choices": [{"message": {"content": "这是一段测试语音"}}]}

    monkeypatch.setattr(me, "_post_chat", fake_post)

    wav = tmp_path / "a.wav"
    wav.write_bytes(_WAV_BYTES)
    result = asyncio.run(me.transcribe_media(wav, "audio", "a.wav"))
    assert result.text == "这是一段测试语音"
    assert result.meta["extractor"] == "none"
    assert result.meta["segments"] == 1


def test_transcribe_video_without_ffmpeg_fails(monkeypatch, tmp_path):
    # 视频必须先解封装出音轨，缺 ffmpeg 只能明确报错（不能产出空知识）
    monkeypatch.setattr(me, "_resolve_ffmpeg", lambda: None)
    video = tmp_path / "a.mp4"
    video.write_bytes(_WAV_BYTES)
    with pytest.raises(me.MediaExtractError) as exc:
        asyncio.run(me.transcribe_media(video, "video", "a.mp4"))
    assert "ffmpeg" in str(exc.value)


def test_transcribe_audio_with_segments(monkeypatch, tmp_path):
    # 有 ffmpeg 且时长超过单段上限 → 应切片、逐段转写并拼接
    monkeypatch.setattr(me, "_resolve_ffmpeg", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(get_settings(), "MEDIA_ASR_SEGMENT_SECONDS", 60)

    async def fake_probe(ffmpeg, src):
        return 150.0  # 150s / 60s → 3 段

    async def fake_to_wav(ffmpeg, src, dst):
        dst.write_bytes(_WAV_BYTES)

    async def fake_split(ffmpeg, src, out_dir, seconds):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for idx in range(3):
            path = out_dir / f"seg_{idx:04d}.wav"
            path.write_bytes(_WAV_BYTES)
            paths.append(path)
        return paths

    calls = {"n": 0}

    async def fake_post(payload):
        calls["n"] += 1
        return {"choices": [{"message": {"content": f"第{calls['n']}段"}}]}

    monkeypatch.setattr(me, "_probe_duration", fake_probe)
    monkeypatch.setattr(me, "_to_wav_16k", fake_to_wav)
    monkeypatch.setattr(me, "_split_wav", fake_split)
    monkeypatch.setattr(me, "_post_chat", fake_post)

    src = tmp_path / "a.mp3"
    src.write_bytes(_WAV_BYTES)
    result = asyncio.run(me.transcribe_media(src, "audio", "a.mp3"))
    assert calls["n"] == 3
    assert result.text == "第1段\n第2段\n第3段"
    assert result.meta["segments"] == 3
    assert result.meta["duration_seconds"] == 150.0
    assert result.meta["extractor"] == "ffmpeg"


def test_transcribe_audio_too_long_rejected(monkeypatch, tmp_path):
    # 超过「段数上限 × 单段时长」的媒体明确拒绝，避免费用与时延失控
    monkeypatch.setattr(me, "_resolve_ffmpeg", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(get_settings(), "MEDIA_ASR_SEGMENT_SECONDS", 60)
    monkeypatch.setattr(get_settings(), "MEDIA_ASR_MAX_SEGMENTS", 2)

    async def fake_probe(ffmpeg, src):
        return 600.0  # 10 段 > 上限 2

    async def fake_to_wav(ffmpeg, src, dst):
        dst.write_bytes(_WAV_BYTES)

    async def fake_split(ffmpeg, src, out_dir, seconds):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for idx in range(10):
            path = out_dir / f"seg_{idx:04d}.wav"
            path.write_bytes(_WAV_BYTES)
            paths.append(path)
        return paths

    monkeypatch.setattr(me, "_probe_duration", fake_probe)
    monkeypatch.setattr(me, "_to_wav_16k", fake_to_wav)
    monkeypatch.setattr(me, "_split_wav", fake_split)

    src = tmp_path / "long.wav"
    src.write_bytes(_WAV_BYTES)
    with pytest.raises(me.MediaExtractError) as exc:
        asyncio.run(me.transcribe_media(src, "audio", "long.wav"))
    assert "上限" in str(exc.value)


def test_transcribe_audio_empty_result_raises(monkeypatch, tmp_path):
    # 无人声（模型返回空）时明确失败，避免产生「已入库但无内容」的假成功
    monkeypatch.setattr(me, "_resolve_ffmpeg", lambda: None)

    async def fake_post(payload):
        return {"choices": [{"message": {"content": ""}}]}

    monkeypatch.setattr(me, "_post_chat", fake_post)
    wav = tmp_path / "silent.wav"
    wav.write_bytes(_WAV_BYTES)
    with pytest.raises(me.MediaExtractError) as exc:
        asyncio.run(me.transcribe_media(wav, "audio", "silent.wav"))
    assert "未识别到有效语音" in str(exc.value)


# =====================================================================
# 4. 提取文本 → ParsedBlock
# =====================================================================
def test_parse_extracted_text_splits_paragraphs():
    blocks = parse_extracted_text("第一段\n\n第二段\n继续第二段", heading="图片文字")
    assert len(blocks) == 1
    assert blocks[0].heading == "图片文字"
    # 空行分段；段内单换行保留
    assert blocks[0].paragraphs == ["第一段", "第二段\n继续第二段"]


def test_parse_extracted_text_empty_raises():
    with pytest.raises(DocumentParseError):
        parse_extracted_text("   \n\n  ")


def test_parse_document_rejects_media_types():
    # 媒体类型必须先经 OCR/ASR，不能直接走文本解析器（避免误用）
    from app.services.parser import parse_document

    with pytest.raises(DocumentParseError) as exc:
        parse_document(b"whatever", "image")
    assert "OCR/ASR" in str(exc.value)


# =====================================================================
# 5. 统一入口分发
# =====================================================================
def test_extract_media_dispatches_by_type(monkeypatch, tmp_path):
    calls = []

    async def fake_ocr(content, filename=""):
        calls.append(("ocr", filename))
        return me.MediaExtraction(text="图片文字")

    async def fake_asr(path, file_type, filename=""):
        calls.append((file_type, filename))
        return me.MediaExtraction(text="转写文字")

    monkeypatch.setattr(me, "ocr_image", fake_ocr)
    monkeypatch.setattr(me, "transcribe_media", fake_asr)

    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    assert asyncio.run(me.extract_media(image, "image", "a.png")).text == "图片文字"

    audio = tmp_path / "a.wav"
    audio.write_bytes(_WAV_BYTES)
    assert asyncio.run(me.extract_media(audio, "audio", "a.wav")).text == "转写文字"

    with pytest.raises(me.MediaExtractError):
        asyncio.run(me.extract_media(audio, "word", "a.wav"))

    assert ("ocr", "a.png") in calls
    assert ("audio", "a.wav") in calls


# =====================================================================
# 6. 能力快照
# =====================================================================
def test_capabilities_shape():
    caps = me.capabilities()
    assert set(caps) >= {"enabled", "ocr", "asr", "ocr_model", "asr_model", "ffmpeg"}
    # 模型名必须来自配置，便于 /health 一眼看出部署用的是哪个模型
    assert caps["ocr_model"] == get_settings().MEDIA_OCR_MODEL
    assert caps["asr_model"] == get_settings().MEDIA_ASR_MODEL
