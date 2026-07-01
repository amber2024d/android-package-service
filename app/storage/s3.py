"""S3 / 兼容对象存储后端（阶段 22）。boto3 懒加载；client 可注入（单测传假 client，无需装 boto3）。

signed URL 走 generate_presigned_url('get_object', ...)，带 ResponseContentDisposition 保下载文件名；
s3_endpoint_url 存在即兼容 MinIO 等自建端点。桶由运维预置，应用不建桶（§6.6）。
"""

import json
from collections.abc import Iterator
from pathlib import Path

from app.storage.base import METADATA_NAME, ObjectMeta, StorageBackend


class S3Backend(StorageBackend):
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "artifacts",
        region: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client=None,
    ):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client or self._build_client(region, endpoint_url, access_key_id, secret_access_key)

    @staticmethod
    def _build_client(region, endpoint_url, access_key_id, secret_access_key):
        import boto3  # 懒加载：仅生产建真实 client 时需要 boto3

        return boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def upload(self, local_path: Path, key: str) -> None:
        self._client.upload_file(str(local_path), self.bucket, self._full(key))

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def head(self, key: str) -> ObjectMeta | None:
        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=self._full(key))
        except Exception:  # noqa: BLE001 — 缺失/网络异常一律视作不存在，触发重下
            return None
        return ObjectMeta(size=resp["ContentLength"], etag=resp.get("ETag"), last_modified=str(resp.get("LastModified")) or None)

    def signed_url(self, key: str, *, expires_in: int, filename: str) -> str | None:
        return self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": self._full(key),
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=expires_in,
        )

    def open_stream(self, key: str) -> Iterator[bytes]:
        body = self._client.get_object(Bucket=self.bucket, Key=self._full(key))["Body"]
        while chunk := body.read(1024 * 1024):
            yield chunk

    def get_metadata(self, prefix: str) -> dict | None:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._full(f"{prefix}/{METADATA_NAME}"))
        except Exception:  # noqa: BLE001
            return None
        return json.loads(resp["Body"].read())

    def put_metadata(self, prefix: str, meta: dict) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._full(f"{prefix}/{METADATA_NAME}"),
            Body=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
