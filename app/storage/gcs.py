"""Google Cloud Storage 后端（阶段 22）。google-cloud-storage 懒加载；bucket 可注入（单测传假 bucket）。

signed URL 走 v4 generate_signed_url（**必须**带私钥的服务账号 GCS_CREDENTIALS_JSON；纯 ADC/Workload Identity
无私钥须走 IAM SignBlob，本期默认要求 SA key，§4.8），带 response_disposition 保下载文件名。桶由运维预置。
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from app.storage.base import METADATA_NAME, ObjectMeta, StorageBackend


class GCSBackend(StorageBackend):
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "artifacts",
        credentials_json: str | None = None,
        bucket_client=None,
    ):
        self.prefix = prefix.strip("/")
        self._bucket = bucket_client or self._build_bucket(bucket, credentials_json)

    @staticmethod
    def _build_bucket(bucket_name: str, credentials_json: str | None):
        from google.cloud import storage  # 懒加载：仅生产建真实 client 时需要 SDK

        if credentials_json and credentials_json.strip().startswith("{"):
            client = storage.Client.from_service_account_info(json.loads(credentials_json))
        elif credentials_json:
            client = storage.Client.from_service_account_json(credentials_json)
        else:
            client = storage.Client()  # ADC（无私钥则 signed URL 需 IAM SignBlob，见 §4.8）
        return client.bucket(bucket_name)

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _blob(self, key: str):
        return self._bucket.blob(self._full(key))

    def upload(self, local_path: Path, key: str) -> None:
        self._blob(key).upload_from_filename(str(local_path))

    def exists(self, key: str) -> bool:
        return self._blob(key).exists()

    def head(self, key: str) -> ObjectMeta | None:
        blob = self._blob(key)
        if not blob.exists():
            return None
        blob.reload()
        return ObjectMeta(size=blob.size, etag=getattr(blob, "etag", None))

    def signed_url(self, key: str, *, expires_in: int, filename: str) -> str | None:
        return self._blob(key).generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expires_in),
            response_disposition=f'attachment; filename="{filename}"',
        )

    def open_stream(self, key: str) -> Iterator[bytes]:
        yield self._blob(key).download_as_bytes()

    def get_metadata(self, prefix: str) -> dict | None:
        blob = self._bucket.blob(self._full(f"{prefix}/{METADATA_NAME}"))
        if not blob.exists():
            return None
        return json.loads(blob.download_as_bytes())

    def put_metadata(self, prefix: str, meta: dict) -> None:
        blob = self._bucket.blob(self._full(f"{prefix}/{METADATA_NAME}"))
        blob.upload_from_string(json.dumps(meta, ensure_ascii=False), content_type="application/json")
