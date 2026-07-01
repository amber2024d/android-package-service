"""S3Backend 单测：注入假 boto3 client（不装 boto3、不触真实云），覆盖 upload/exists/head/signed_url/元数据/复用。"""

import json
import zipfile
from pathlib import Path

from app.domain.models import DownloadPlan
from app.download.artifact_store import ArtifactStore
from app.download.verifier import FileVerifier
from app.storage.base import StorageBackend
from app.storage.s3 import S3Backend


class _NotFound(Exception):
    pass


class _Body:
    def __init__(self, data: bytes):
        self._data = data
        self._read = False

    def read(self, _size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._data


class _FakeS3Client:
    def __init__(self):
        self.store: dict[tuple[str, str], bytes] = {}

    def upload_file(self, filename, Bucket, Key):
        self.store[(Bucket, Key)] = Path(filename).read_bytes()

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.store[(Bucket, Key)] = Body

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.store:
            raise _NotFound()
        return {"ContentLength": len(self.store[(Bucket, Key)]), "ETag": '"etag"', "LastModified": "2026-07-01"}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.store:
            raise _NotFound()
        return {"Body": _Body(self.store[(Bucket, Key)])}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return (
            f"https://s3.fake/{Params['Bucket']}/{Params['Key']}"
            f"?X-Amz-Expires={ExpiresIn}&disp={Params['ResponseContentDisposition']}"
        )


def _plan() -> DownloadPlan:
    return DownloadPlan(package_name="com.x", app_name="X", version_name="1.0", version_code=5, provider="s3src", files=[])


def _xapk(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"package_name": "com.x", "version_name": "1.0", "version_code": 5}))
        archive.writestr("base.apk", b"PK\x03\x04base")
    return path


def _backend(client) -> S3Backend:
    return S3Backend(bucket="pkgs", prefix="artifacts", client=client)


def test_upload_head_exists_with_prefix(tmp_path):
    client = _FakeS3Client()
    backend = _backend(client)
    staged = _xapk(tmp_path / "app.xapk")
    key = StorageBackend.object_key(_plan(), "app.xapk")
    backend.upload(staged, key)
    # 桶内落在 prefix/ 之下
    assert ("pkgs", f"artifacts/{key}") in client.store
    assert backend.exists(key)
    assert backend.head(key).size == staged.stat().st_size
    assert backend.head("missing/x.apk") is None
    assert backend.local_path(key) is None


def test_signed_url_has_disposition_and_ttl(tmp_path):
    backend = _backend(_FakeS3Client())
    url = backend.signed_url("s3src/com.x/5/app.xapk", expires_in=900, filename="app.xapk")
    assert "X-Amz-Expires=900" in url
    assert 'attachment; filename="app.xapk"' in url
    assert "artifacts/s3src/com.x/5/app.xapk" in url


def test_metadata_roundtrip_and_reuse(tmp_path):
    client = _FakeS3Client()
    backend = _backend(client)
    store = ArtifactStore(backend, FileVerifier())
    plan = _plan()
    staged = _xapk(tmp_path / "app.xapk")
    key = StorageBackend.object_key(plan, "app.xapk")
    store.commit(plan, staged, key)
    assert backend.get_metadata(backend.metadata_prefix(plan))["key"] == key
    # 对象后端复用：元数据 + head 大小一致
    assert store.existing(_plan()) == key
