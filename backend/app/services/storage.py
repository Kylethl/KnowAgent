# 对象存储适配层（P1-B2）
# ---------------------------------------------------------------------
# 职责：把「原始文件落盘到对象存储」这件事抽象成统一接口，屏蔽 MinIO / 本地目录
#       的差异，满足《云存储测试方案》中「存储可替换、本地 MinIO 与云上行为一致」的原则。
#
# 设计要点：
#   1. 统一接口 BaseStorage：put_object / get_object / delete_object（异步方法）。
#   2. 两种实现：
#      - LocalStorage：写到本地目录，开发期零依赖即可跑通上传闭环（免 MinIO）。
#      - MinioStorage：使用官方 minio SDK（同步），经 asyncio.to_thread 异步化；
#        SDK 采用懒导入，仅当 STORAGE_BACKEND=minio 且实例化时才需要 minio 包。
#   3. build_storage() 工厂根据配置返回对应实现；DB 只存「相对 key」，
#      换桶/换后端都不影响数据库记录（见《云存储测试方案》路径命名建议）。
#
# 路径命名：{scope}/{yyyy}/{mm}/{uuid}.{ext}，scope 为用户隔离维度（匿名用 anonymous）。

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 本地落盘根目录的解析基准：app/services/storage.py → parents[2] = backend 目录
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class StorageError(Exception):
    """存储层异常：对象写入 / 删除失败的统一异常类型。

    上层（api/documents.py）捕获后转为 5xx 并给出明确提示，且**不写 DB 脏记录**。
    """

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        # 默认 502（Bad Gateway，视为下游存储不可用）；也可由调用方指定（如 503）
        self.status_code = status_code


class BaseStorage(ABC):
    """对象存储统一抽象。

    所有实现都必须保证：相同 key 的 put_object 是幂等的（重复上传不会互相破坏），
    且 delete_object 对「已不存在的对象」保持静默成功（便于失败补偿重试）。
    """

    @abstractmethod
    async def put_object(self, key: str, data: bytes, content_type: str | None) -> None:
        """写入一个对象。

        Args:
            key: 相对对象键（不含 bucket），由 build_object_key 生成。
            data: 文件二进制内容。
            content_type: MIME 类型（可为空，由后端尽力填写）。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_object(self, key: str) -> bytes:
        """读取一个对象（P3-B1 管理员预览文档内容用）。

        Args:
            key: 相对对象键（不含 bucket）。

        Returns:
            对象二进制内容。

        Raises:
            StorageError: 对象不存在或读取失败（由具体实现决定，通常转 404/502）。
        """
        raise NotImplementedError

    @abstractmethod
    async def delete_object(self, key: str) -> None:
        """删除一个对象（补偿删除用）。

        即使对象不存在也不报错（Maxy 设计：补偿重试安全）。
        """
        raise NotImplementedError


class LocalStorage(BaseStorage):
    """本地目录存储（开发默认，零依赖）。

    文件落到 <STORAGE_LOCAL_DIR>/<key>；key 自带 scope/日期/uuid 目录层级，
    天然避免覆盖与跨用户可见。生产可把该目录指向挂载卷或对象存储同步目录。
    """

    def __init__(self, root: str | Path):
        # 绝对路径直接使用；相对路径以 backend 目录为基准解析
        self.root = Path(root)
        if not self.root.is_absolute():
            self.root = _BACKEND_ROOT / self.root
        self.root.mkdir(parents=True, exist_ok=True)
        logger.info("LocalStorage 初始化：落盘根目录=%s", self.root)

    async def put_object(self, key: str, data: bytes, content_type: str | None) -> None:
        # key 可能含 "/" 层级，先创建父目录；用临时文件 + 重命名保证原子写入
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")

        def _write() -> None:
            # 阻塞 IO 放到线程池，避免阻塞事件循环
            tmp.write_bytes(data)
            tmp.replace(target)  # 原子替换，避免读到半截文件
            if tmp.exists():
                tmp.unlink()

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:  # 磁盘满 / 权限不足等
            raise StorageError(f"本地文件写入失败：{exc}") from exc
        logger.debug("LocalStorage 写入成功：%s（%d 字节）", target, len(data))

    async def get_object(self, key: str) -> bytes:
        """读取本地落盘的对象内容（管理员预览用）。

        路径穿越防护：解析后校验目标仍在 root 之内，避免 key 含 ../ 读到系统文件。
        """
        target = (self.root / key).resolve()
        root = self.root.resolve()
        # 校验解析后的绝对路径仍在根目录内（含根目录自身）
        if not (target == root or root in target.parents):
            raise StorageError(f"非法的对象键：{key}", status_code=400)

        def _read() -> bytes:
            return target.read_bytes()

        try:
            return await asyncio.to_thread(_read)
        except FileNotFoundError as exc:
            raise StorageError(f"对象不存在：{key}", status_code=404) from exc
        except OSError as exc:
            raise StorageError(f"本地文件读取失败：{exc}") from exc

    async def delete_object(self, key: str) -> None:
        target = self.root / key

        def _remove() -> None:
            # 文件不存在也静默成功，保证补偿删除可重试
            if target.exists():
                target.unlink()

        try:
            await asyncio.to_thread(_remove)
        except OSError as exc:
            raise StorageError(f"本地文件删除失败：{exc}") from exc
        logger.debug("LocalStorage 删除完成：%s", target)


class MinioStorage(BaseStorage):
    """MinIO / S3 兼容存储（生产目标，遵循《需求清单》对象存储选型）。

    使用官方 minio SDK（同步实现），通过 asyncio.to_thread 在独立线程执行网络 IO，
    避免阻塞异步事件循环。
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "",
        secure: bool = False,  # 本地 MinIO 多为 http；生产启用 TLS 时改 True
    ):
        # 懒导入：仅 minio 后端真正使用时才需要 minio 包，
        # 否则本地开发不安装该包也能正常 import 本模块。
        try:
            from minio import Minio  # noqa: WPS433 —— 仅此处需要，函数内导入避免硬依赖
        except ImportError as exc:  # 未安装 minio 却选择 minio 后端
            raise StorageError(
                "未安装 minio 包，无法使用 MinIO 存储后端。"
                "请 pip install minio 或把 STORAGE_BACKEND 改为 local。"
            ) from exc

        self.bucket = bucket
        self._client = Minio(
            endpoint,
            access_key=access_key or None,
            secret_key=secret_key or None,
            region=region or None,
            secure=secure,
        )
        # 自动建桶：桶不存在则创建（并发安全：重复创建会被 SDK 忽略）
        self._ensure_bucket()
        logger.info("MinioStorage 初始化：endpoint=%s，bucket=%s", endpoint, bucket)

    def _ensure_bucket(self) -> None:
        """确保目标桶存在；不存在则创建（忽略「已存在」类错误）。"""
        try:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)
        except Exception as exc:  # 凭证错误 / 网络不可达 / 权限不足等
            raise StorageError(f"MinIO 初始化失败（检查 endpoint / 凭证 / 桶）：{exc}") from exc

    async def put_object(self, key: str, data: bytes, content_type: str | None) -> None:
        def _put() -> None:
            # minio SDK 的 put_object 接受 bytes 流或 BytesIO；这里用 BytesIO 便于携带长度
            from io import BytesIO

            self._client.put_object(
                self.bucket,
                key,
                BytesIO(data),
                length=len(data),
                content_type=content_type or "application/octet-stream",
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:  # 网络抖动 / 凭证失效 / 桶无写权限等
            raise StorageError(f"MinIO 对象写入失败：{exc}") from exc
        logger.debug("MinioStorage 写入成功：bucket=%s，key=%s（%d 字节）", self.bucket, key, len(data))

    async def get_object(self, key: str) -> bytes:
        def _get() -> bytes:
            from io import BytesIO

            # get_object 返回的是可关闭的流，这里一次性读进内存（文档最大 20MB，可接受）
            resp = self._client.get_object(self.bucket, key)
            try:
                return BytesIO(resp.read()).getvalue()
            finally:
                resp.close()
                resp.release_conn()

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # 对象不存在 / 网络抖动 / 凭证失效等
            raise StorageError(f"MinIO 对象读取失败：{exc}") from exc

    async def delete_object(self, key: str) -> None:
        def _delete() -> None:
            # remove_object 对不存在的对象会抛错，这里吞掉以保证补偿可重试
            self._client.remove_object(self.bucket, key)

        try:
            await asyncio.to_thread(_delete)
        except Exception as exc:
            raise StorageError(f"MinIO 对象删除失败：{exc}") from exc
        logger.debug("MinioStorage 删除完成：bucket=%s，key=%s", self.bucket, key)


def build_object_key(scope: str | None, ext: str) -> str:
    """生成对象存储的相对 key（不含 bucket）。

    结构：{scope}/{yyyy}/{mm}/{uuid}.{ext}
      - scope：用户隔离维度（user_id 或 anonymous），保证不同用户目录互不可见。
      - yyyy/mm：按上传月份分目录，便于生命周期管理与对账。
      - uuid：每次上传唯一，杜绝同名覆盖（见《云存储测试方案》ST-U09 / ST-U10）。

    Args:
        scope: 用户标识；为空或 None 时视为匿名（anonymous）。
        ext: 文件扩展名（**不含点**，如 "docx" / "md"）。
    """
    now = datetime.now()
    # 兼容 UUID 入参：调用方（services/documents.py）传的是 _parse_user_id() 的结果，
    # 其类型为 Optional[UUID]。P1 无鉴权时该值为 None（走 anonymous）；
    # P2-B2 鉴权后必为 UUID，直接 .strip() 会抛 AttributeError，故此处统一转字符串。
    safe_scope = str(scope).strip() if scope else "anonymous"
    safe_scope = safe_scope or "anonymous"
    # 去掉 scope 中可能混入的路径分隔符，防止目录穿越
    safe_scope = safe_scope.replace("/", "_").replace("\\", "_")
    return f"{safe_scope}/{now.year:04d}/{now.month:02d}/{uuid4().hex}.{ext}"


def build_storage(settings=None) -> BaseStorage:
    """根据配置构造存储后端实例（工厂函数）。

    Args:
        settings: 配置对象；缺省读取全局单例（get_settings()）。

    Returns:
        BaseStorage 的具体实现（LocalStorage 或 MinioStorage）。
    """
    if settings is None:
        settings = get_settings()

    backend = (settings.STORAGE_BACKEND or "local").strip().lower()
    if backend == "minio":
        return MinioStorage(
            endpoint=settings.STORAGE_ENDPOINT,
            access_key=settings.STORAGE_ACCESS_KEY,
            secret_key=settings.STORAGE_SECRET_KEY,
            bucket=settings.STORAGE_BUCKET,
            region=settings.STORAGE_REGION,
            secure=False,  # 本地/内网 MinIO 多为 http；生产 TLS 请改 True
        )

    # 默认 / 其它值一律走本地目录（最稳妥的零依赖开发后端）
    return LocalStorage(root=settings.STORAGE_LOCAL_DIR)
