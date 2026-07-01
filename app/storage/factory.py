"""存储后端工厂（阶段 21）：按 settings.storage_backend 选后端。gcs/s3 在阶段 22 补齐。"""

from app.core.config import Settings
from app.storage.base import StorageBackend
from app.storage.local import LocalStorageBackend


def build_storage_backend(settings: Settings) -> StorageBackend:
    backend = settings.storage_backend
    if backend == "local":
        # 本地根 = artifacts_dir（已含 artifacts/ 段）；storage_prefix 仅对云后端生效。
        return LocalStorageBackend(settings.artifacts_dir)
    if backend == "gcs":
        if not settings.gcs_bucket:
            raise RuntimeError("STORAGE_BACKEND=gcs 需要配置 GCS_BUCKET。")
        from app.storage.gcs import GCSBackend

        return GCSBackend(
            bucket=settings.gcs_bucket,
            prefix=settings.storage_prefix,
            credentials_json=settings.gcs_credentials_json,
        )
    if backend == "s3":
        if not settings.s3_bucket:
            raise RuntimeError("STORAGE_BACKEND=s3 需要配置 S3_BUCKET。")
        from app.storage.s3 import S3Backend

        return S3Backend(
            bucket=settings.s3_bucket,
            prefix=settings.storage_prefix,
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    raise ValueError(f"未知存储后端：{backend}（可选 local/gcs/s3）")
