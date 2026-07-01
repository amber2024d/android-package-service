"""产物对象存储（阶段 21–22）：StorageBackend 工厂策略（local / gcs / s3）+ signed URL 下发。"""

from app.storage.base import ObjectMeta, StorageBackend
from app.storage.factory import build_storage_backend
from app.storage.local import LocalStorageBackend

__all__ = ["StorageBackend", "ObjectMeta", "LocalStorageBackend", "build_storage_backend"]
