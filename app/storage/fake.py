"""内存对象存储桩（阶段 21）——供单测无云验证 upload/exists/head/signed_url/元数据边车与 signed URL 下发。

模拟对象后端：`local_path` 返回 None（走 signed URL 302），默认会签名（返回假 URL），
便于在没有真实 GCS/S3 的情况下覆盖「对象后端 → 302 signed URL」下发路径。参照 `app/providers/fake.py`。
"""

from collections.abc import Iterator
from pathlib import Path

from app.storage.base import ObjectMeta, StorageBackend


class FakeStorageBackend(StorageBackend):
    def __init__(self, *, sign: bool = True):
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict] = {}
        self._sign = sign

    def upload(self, local_path: Path, key: str) -> None:
        self.objects[key] = Path(local_path).read_bytes()

    def exists(self, key: str) -> bool:
        return key in self.objects

    def head(self, key: str) -> ObjectMeta | None:
        data = self.objects.get(key)
        return ObjectMeta(size=len(data)) if data is not None else None

    def signed_url(self, key: str, *, expires_in: int, filename: str) -> str | None:
        if not self._sign:
            return None
        return f"https://fake-storage.local/{key}?expires={expires_in}&filename={filename}"

    def open_stream(self, key: str) -> Iterator[bytes]:
        yield self.objects[key]

    def get_metadata(self, prefix: str) -> dict | None:
        return self.metadata.get(prefix)

    def put_metadata(self, prefix: str, meta: dict) -> None:
        self.metadata[prefix] = meta
