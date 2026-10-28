# P5 功能1「多模态提交」测试
# ---------------------------------------------------------------------
# 覆盖三块：
#   1. 类型识别与魔数校验（services/media_types.py）——纯函数，无外部依赖
#   2. 上传校验（services/documents.validate_upload）——按类型分级的大小上限、
#      扩展名/类型提示不一致、伪装文件拒收
#   3. 上传接口契约（POST /documents/upload）——图片成功落盘落库、伪装文件 415、
#      视频超限 413（在写存储之前就拒绝）
#
# 说明：与 tests/test_documents.py 一致，DB 用 FakeSession 替身、存储用 LocalStorage
#      指向临时目录，不依赖真实 PostgreSQL / MinIO。

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.documents import get_storage_dep
from app.config import get_settings
from app.core.security import get_current_user_id
from app.db.engine import get_db
from app.main import app
from app.services import media_types
from app.services.documents import UploadValidationError, validate_upload
from app.services.storage import LocalStorage

# 真实文件头样本（只需前若干字节，足以触发魔数判定）
PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 16
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 16
WAV_HEAD = b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 16
MP4_HEAD = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16
ZIP_HEAD = b"PK\x03\x04" + b"\x00" * 24


# =====================================================================
# 1. 类型识别与魔数校验
# =====================================================================
@pytest.mark.parametrize(
    "ext,expected",
    [
        ("docx", "word"),
        ("md", "markdown"),
        ("png", "image"),
        ("jpeg", "image"),
        ("mp3", "audio"),
        ("m4a", "audio"),
        ("mp4", "video"),
        ("mkv", "video"),
    ],
)
def test_resolve_file_type(ext, expected):
    assert media_types.resolve_file_type(ext) == expected


def test_resolve_file_type_unknown():
    # 白名单外扩展名（如 .txt）应返回 None，由校验层以 415 拒绝
    assert media_types.resolve_file_type("txt") is None


def test_is_media_type():
    assert media_types.is_media_type("image") is True
    assert media_types.is_media_type("audio") is True
    assert media_types.is_media_type("video") is True
    # 文档类不算多模态（不需要 OCR/ASR）
    assert media_types.is_media_type("word") is False


@pytest.mark.parametrize(
    "head,ext,expected",
    [
        (PNG_HEAD, "png", True),
        (JPEG_HEAD, "jpg", True),
        (WAV_HEAD, "wav", True),
        (MP4_HEAD, "mp4", True),
        # 魔数与扩展名不符：把 zip 改名成 .png（最常见的伪装）
        (ZIP_HEAD, "png", False),
        (PNG_HEAD, "mp4", False),
        # 无法判定（头部无任何已知特征）→ None，按不拦截处理
        (b"\x00" * 32, "png", None),
    ],
)
def test_verify_magic(head, ext, expected):
    assert media_types.verify_magic(head, ext) is expected


def test_guess_mime_prefers_extension_mapping():
    # 浏览器对 .md / .m4a 常给 application/octet-stream，应以扩展名映射为准
    assert media_types.guess_mime("md", "application/octet-stream") == "text/markdown"
    assert media_types.guess_mime("m4a") == "audio/mp4"
    # 未知扩展名回退到上传方给的 Content-Type
    assert media_types.guess_mime("xyz", "application/x-custom") == "application/x-custom"


# =====================================================================
# 2. 上传校验
# =====================================================================
def test_validate_accepts_image():
    ext, doc_type = validate_upload("截图.PNG", 1024, client_type_hint="image", head=PNG_HEAD)
    assert ext == "png"
    assert doc_type == "image"


def test_validate_accepts_audio_and_video():
    assert validate_upload("会议录音.m4a", 1024, head=MP4_HEAD)[1] == "audio"
    assert validate_upload("演示.mp4", 1024, head=MP4_HEAD)[1] == "video"


def test_validate_rejects_image_over_type_limit(monkeypatch):
    # 图片上限压到 10 字节，11 字节即超限 → 413（按类型分级，不共用文档上限）
    settings = get_settings()
    monkeypatch.setattr(settings, "MAX_IMAGE_SIZE", 10)
    with pytest.raises(UploadValidationError) as exc:
        validate_upload("a.png", 11, head=PNG_HEAD)
    assert exc.value.status_code == 413


def test_validate_video_uses_its_own_limit(monkeypatch):
    # 视频上限 200MB：21MB 的视频不应被文档上限（20MB）误伤
    settings = get_settings()
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE", 20)
    ext, doc_type = validate_upload("big.mp4", 21 * 1024 * 1024, head=MP4_HEAD)
    assert (ext, doc_type) == ("mp4", "video")


def test_validate_rejects_spoofed_image():
    # 扩展名 .png 但内容是 zip → 415（防止「改名伪装」）
    with pytest.raises(UploadValidationError) as exc:
        validate_upload("fake.png", 1024, head=ZIP_HEAD)
    assert exc.value.status_code == 415
    assert "不符" in str(exc.value)


def test_validate_rejects_type_mismatch_for_media():
    # 文件名是 .png（image）但前端声明 video → 400
    with pytest.raises(UploadValidationError) as exc:
        validate_upload("a.png", 1024, client_type_hint="video", head=PNG_HEAD)
    assert exc.value.status_code == 400


def test_validate_skips_magic_when_head_absent():
    # 不提供文件头（历史调用方式）时不做魔数校验，只校验扩展名与大小
    ext, doc_type = validate_upload("a.png", 1024)
    assert (ext, doc_type) == ("png", "image")


# =====================================================================
# 3. 上传接口契约
# =====================================================================
class FakeSession:
    """替身异步会话：只模拟 add/commit/refresh（上传链路不做查询）。"""

    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        if obj.id is None:
            obj.id = uuid4()
        if obj.created_at is None:
            obj.created_at = datetime.now(timezone.utc)

    async def rollback(self):
        self.committed = False

    async def execute(self, *args, **kwargs):
        raise NotImplementedError("FakeSession 未模拟查询：上传链路不应发起查询")


_FAKE_USER_ID = uuid4()


@pytest.fixture
def client_and_fakes(tmp_path):
    """注入 FakeSession + LocalStorage，并覆盖鉴权依赖为固定用户。"""
    fake_db = FakeSession()
    store = LocalStorage(root=tmp_path / "uploads")

    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_storage_dep] = lambda: store
    app.dependency_overrides[get_current_user_id] = lambda: _FAKE_USER_ID

    yield TestClient(app), fake_db, tmp_path / "uploads"

    app.dependency_overrides.clear()


def test_upload_image_success(client_and_fakes):
    client, fake_db, store_root = client_and_fakes
    content = PNG_HEAD + b"\x00" * 512

    resp = client.post(
        "/documents/upload",
        files={"file": ("架构图.png", content, "image/png")},
        data={"type": "image"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "image"
    assert body["status"] == "pending"
    assert body["size"] == len(content)
    # 对象已落盘且内容一致
    obj_path = store_root / body["storage_path"]
    assert obj_path.exists()
    assert obj_path.read_bytes() == content
    # 元数据落库：file_type 为 image，MIME 以扩展名映射为准
    assert fake_db.committed is True
    assert fake_db.added[0].file_type == "image"
    assert fake_db.added[0].mime_type == "image/png"


def test_upload_rejects_spoofed_image_over_http(client_and_fakes):
    client, fake_db, store_root = client_and_fakes
    resp = client.post(
        "/documents/upload",
        files={"file": ("fake.png", ZIP_HEAD + b"\x00" * 64, "image/png")},
        data={"type": "image"},
    )
    assert resp.status_code == 415
    # 非法请求不得落盘、不得落库
    assert fake_db.committed is False
    assert not any(store_root.rglob("*.png"))


def test_upload_video_over_limit(client_and_fakes, monkeypatch):
    client, _, _ = client_and_fakes
    # 把视频上限压到 1KB，发送 2KB 视频 → 413（在写存储之前中止）
    monkeypatch.setattr(get_settings(), "MAX_VIDEO_SIZE", 1024)
    resp = client.post(
        "/documents/upload",
        files={"file": ("clip.mp4", MP4_HEAD + b"\x00" * 2048, "video/mp4")},
        data={"type": "video"},
    )
    assert resp.status_code == 413
