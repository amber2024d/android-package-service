"""产物对象存储抽象（阶段 21，§4.2/§4.3）。

**同步接口**：本地为文件 IO；云后端（boto3 / google-cloud-storage，阶段 22）本就是同步 SDK，
签名 URL 生成是本地加密操作、无网络。与现有 `PackageDownloader.download`（异步里跑同步 copy/build/verify）
的风格一致，避免把 existing()/下发链路铺成 async。

对象 key 布局 = `{provider}/{package}/{version_key}/{filename}`（不含桶内根前缀，根前缀由各后端按
`storage_prefix` 内部处理；本地根 = `artifacts_dir` 已含 `artifacts/` 段）。元数据边车对象在同前缀下 `metadata.json`。
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.domain.models import DownloadPlan
from app.utils.filenames import safe_part

METADATA_NAME = "metadata.json"


@dataclass(frozen=True)
class ObjectMeta:
    size: int
    etag: str | None = None
    last_modified: str | None = None


class StorageBackend(ABC):
    @staticmethod
    def metadata_prefix(plan: DownloadPlan) -> str:
        return f"{safe_part(plan.provider)}/{safe_part(plan.package_name)}/{safe_part(plan.version_key)}"

    @classmethod
    def object_key(cls, plan: DownloadPlan, filename: str) -> str:
        return f"{cls.metadata_prefix(plan)}/{filename}"

    @abstractmethod
    def upload(self, local_path: Path, key: str) -> None:
        """本地暂存文件 → 后端（幂等/可覆盖）。"""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def head(self, key: str) -> ObjectMeta | None:
        """对象存在则返回 size/etag/last_modified，否则 None。"""

    @abstractmethod
    def signed_url(self, key: str, *, expires_in: int, filename: str) -> str | None:
        """短期 signed URL；本地后端返回 None（下发回退 FileResponse / NAS 直链）。"""

    @abstractmethod
    def open_stream(self, key: str) -> Iterator[bytes]:
        """流式读对象（下发兜底 / 本地 FileResponse 之外的备用路径）。"""

    @abstractmethod
    def get_metadata(self, prefix: str) -> dict | None:
        """读边车元数据 `{prefix}/metadata.json`。"""

    @abstractmethod
    def put_metadata(self, prefix: str, meta: dict) -> None: ...

    def local_path(self, key: str) -> Path | None:
        """本地后端返回真实路径供 FileResponse / NAS 直链；对象后端 None。"""
        return None
