# P1-B2 文档上传接口测试
# ---------------------------------------------------------------------
# 覆盖：
#   1. 校验逻辑（格式白名单 / 双扩展名 / 空文件 / 大小上限 / type 不一致）
#   2. 本地存储适配层（写 → 读 → 删 往返，不依赖 MinIO）
#   3. 接口契约：非法请求在「写存储之前」被拒（415/413/400）；
#      合法请求走通「校验 → 写存储 → 落库」并返回前端契约结构（201）。
#
# 说明：避免依赖真实 PostgreSQL / MinIO，DB 用 FakeSession 替身，
#      存储用 LocalStorage 指向临时目录，符合《云存储测试方案》单元/接口测试策略。

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.documents import get_storage_dep
from app.config import get_settings
from app.core.security import get_current_user_id
from app.db.engine import get_db
from app.main import app
from app.services import storage as storage_mod
from app.services.documents import UploadValidationError, validate_upload
from app.services.storage import LocalStorage


# ---------------------------------------------------------------------
# 1. 校验逻辑单测（不触及存储 / 数据库）
# ---------------------------------------------------------------------
def test_validate_rejects_disallowed_extension():
    # .txt 不在白名单 → 415
    with pytest.raises(UploadValidationError) as exc:
        validate_upload("note.txt", 100)
    assert exc.value.status_code == 415


def test_validate_rejects_double_extension():
    # a.md.exe 含危险扩展名 .exe（双扩展名绕过防护）→ 400
    with pytest.raises(UploadValidationError) as exc:
        validate_upload("a.md.exe", 100)
    assert exc.value.status_code == 400


def test_validate_rejects_empty_filename():
    with pytest.raises(UploadValidationError) as exc:
        validate_upload("", 100)
    assert exc.value.status_code == 400


def test_validate_rejects_empty_file(monkeypatch):
    # 0 字节文件 → 400
    with pytest.raises(UploadValidationError) as exc:
        validate_upload("doc.md", 0)
    assert exc.value.status_code == 400


def test_validate_rejects_oversize(monkeypatch):
    # 把上限临时压到 5 字节，10 字节即超限 → 413
    settings = get_settings()
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE", 5)
    with pytest.raises(UploadValidationError) as exc:
        validate_upload("doc.md", 10)
    assert exc.value.status_code == 413


def test_validate_rejects_type_mismatch():
    # 文件名是 .md（推导 markdown）但前端声明 word → 400（防篡改/误传）
    with pytest.raises(UploadValidationError) as exc:
        validate_upload("doc.md", 100, client_type_hint="word")
    assert exc.value.status_code == 400


def test_validate_accepts_valid_markdown():
    # 合法 .md → 返回 (ext, doc_type)
    ext, doc_type = validate_upload("报告.md", 100, client_type_hint="markdown")
    assert ext == "md"
    assert doc_type == "markdown"


def test_validate_accepts_valid_docx_case_insensitive():
    # 大写扩展名也应被接受（大小写不敏感）
    ext, doc_type = validate_upload("Contract.DOCX", 100)
    assert ext == "docx"
    assert doc_type == "word"


# ---------------------------------------------------------------------
# 2. 本地存储适配层往返（写 → 读 → 删）
# ---------------------------------------------------------------------
def test_local_storage_roundtrip(tmp_path):
    store = LocalStorage(root=tmp_path / "uploads")
    key = "anonymous/2026/10/abc.md"

    # 写
    asyncio.run(store.put_object(key, b"hello world", "text/markdown"))
    written = tmp_path / "uploads" / key
    assert written.exists()
    assert written.read_bytes() == b"hello world"

    # 删（幂等：再次删除不报错）
    asyncio.run(store.delete_object(key))
    assert not written.exists()
    asyncio.run(store.delete_object(key))  # 已不存在，静默成功


# ---------------------------------------------------------------------
# 3. 接口契约（FastAPI TestClient）
# ---------------------------------------------------------------------
class FakeSession:
    """替身异步会话：记录 add/commit/refresh，模拟 DB 生成 id 与 created_at。

    说明：P2 给 /documents/upload 加上鉴权后，鉴权依赖会先于业务代码访问数据库
    （查当前用户）。为了让本文件仍然只测「上传链路」，测试里用
    dependency_overrides 覆盖鉴权依赖，不触发真实查询；因此这里的 execute
    只作为兜底实现存在——若将来业务路径真的发起查询，会明确报「FakeSession 不支持」，
    而不是抛一个难以定位的 AttributeError。
    """

    def __init__(self):
        self.added = []
        self.committed = False
        self.refreshed = []

    def add(self, obj):
        # 注意：真实 AsyncSession.add 是同步方法（无需 await），故此处也用同步
        self.added.append(obj)

    async def execute(self, *args, **kwargs):
        """兜底实现：明确告知测试桩未模拟查询，避免误以为测试覆盖了查询逻辑。"""
        raise NotImplementedError(
            "FakeSession 未模拟 execute()：上传链路不应发起查询。"
            "如需测试查询，请为本用例补充桩实现或用真实测试库。"
        )

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        if obj.id is None:
            obj.id = uuid4()
        if obj.created_at is None:
            obj.created_at = datetime.now(timezone.utc)
        self.refreshed.append(obj)

    async def rollback(self):
        self.committed = False


# 测试用固定用户 ID：与 FakeSession.refresh 造出的文档归属保持一致即可，
# 本文件不校验归属（那是 /documents 列表接口与用户隔离测试的职责）。
_FAKE_USER_ID = uuid4()


@pytest.fixture
def client_and_fakes(tmp_path):
    """构造 TestClient 并注入 FakeSession + LocalStorage 依赖。

    P2-B2 起 /documents/upload 要求鉴权（AUTH_REQUIRED=true），
    这里覆盖鉴权依赖注入固定用户，使本文件专注验证上传链路本身；
    鉴权行为由 tests/test_auth.py 覆盖。
    """
    fake_db = FakeSession()
    local_store = LocalStorage(root=tmp_path / "uploads")

    app.dependency_overrides[get_db] = lambda: fake_db
    app.dependency_overrides[get_storage_dep] = lambda: local_store
    app.dependency_overrides[get_current_user_id] = lambda: _FAKE_USER_ID

    # 把存储根目录暴露出来，便于断言对象已落盘
    yield TestClient(app), fake_db, tmp_path / "uploads"

    app.dependency_overrides.clear()


def test_upload_rejects_bad_extension(client_and_fakes):
    client, _, _ = client_and_fakes
    # 发送 .txt → 期望 415（写存储之前被拒）
    resp = client.post(
        "/documents/upload",
        files={"file": ("note.txt", b"some content", "text/plain")},
        data={"type": "markdown"},
    )
    assert resp.status_code == 415
    assert "格式" in resp.json()["detail"]


def test_upload_rejects_oversize(client_and_fakes):
    client, _, _ = client_and_fakes
    # .md 合法但超 20MB 上限（生成 ~21MB 内容）→ 413
    big = b"x" * (21 * 1024 * 1024)
    resp = client.post(
        "/documents/upload",
        files={"file": ("big.md", big, "text/markdown")},
        data={"type": "markdown"},
    )
    assert resp.status_code == 413


def test_upload_success_flow(client_and_fakes):
    client, fake_db, store_root = client_and_fakes
    content = "# 知识库文档\n这是一段示例内容。".encode("utf-8")

    resp = client.post(
        "/documents/upload",
        files={"file": ("入门指南.md", content, "text/markdown")},
        data={"type": "markdown"},
    )

    # 1) 返回 201 且结构对齐前端 DocumentRecord
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "markdown"
    assert body["status"] == "pending"          # P1 上传即待审核
    assert body["size"] == len(content)
    assert body["filename"] == "入门指南.md"
    # P2 起提交人由令牌解析：对象路径按 user_id 隔离（P1 无鉴权时是 anonymous/）。
    # 这里断言落在「当前用户」目录下，等价于验证了「用户间文件不混放」。
    assert body["storage_path"].startswith(f"{_FAKE_USER_ID}/")
    assert "created_at" in body and body["created_at"]

    # 2) 存储侧：对象已落盘到本地目录
    obj_path = store_root / body["storage_path"]
    assert obj_path.exists()
    assert obj_path.read_bytes() == content

    # 3) 数据库侧：元数据已落库（commit 成功）
    assert fake_db.committed is True
    assert len(fake_db.added) == 1
    assert fake_db.added[0].file_type == "markdown"
    assert fake_db.added[0].status == "pending"
