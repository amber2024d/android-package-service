"""GCSBackend 单测：注入假 bucket（不装 google-cloud-storage、不触真实云）。"""

import json
import zipfile
from pathlib import Path

from app.domain.models import DownloadPlan
from app.download.artifact_store import ArtifactStore
from app.download.verifier import FileVerifier
from app.storage.base import StorageBackend
from app.storage.gcs import GCSBackend


class _FakeBlob:
    def __init__(self, store: dict, name: str):
        self._store = store
        self.name = name
        self.size = None
        self.etag = "etag"
        self.last_signed = None

    def upload_from_filename(self, filename):
        self._store[self.name] = Path(filename).read_bytes()

    def upload_from_string(self, data, content_type=None):
        self._store[self.name] = data.encode("utf-8") if isinstance(data, str) else data

    def exists(self):
        return self.name in self._store

    def reload(self):
        self.size = len(self._store.get(self.name, b""))

    def download_as_bytes(self):
        return self._store[self.name]

    def generate_signed_url(self, version, expiration, response_disposition):
        return f"https://gcs.fake/{self.name}?version={version}&ttl={int(expiration.total_seconds())}&disp={response_disposition}"


class _FakeBucket:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self.store, name)


def _plan() -> DownloadPlan:
    return DownloadPlan(package_name="com.x", app_name="X", version_name="1.0", version_code=5, provider="gcssrc", files=[])


def _xapk(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"package_name": "com.x", "version_name": "1.0", "version_code": 5}))
        archive.writestr("base.apk", b"PK\x03\x04base")
    return path


def _backend(bucket) -> GCSBackend:
    return GCSBackend(bucket="pkgs", prefix="artifacts", bucket_client=bucket)


def test_upload_head_exists_with_prefix(tmp_path):
    bucket = _FakeBucket()
    backend = _backend(bucket)
    staged = _xapk(tmp_path / "app.xapk")
    key = StorageBackend.object_key(_plan(), "app.xapk")
    backend.upload(staged, key)
    assert f"artifacts/{key}" in bucket.store
    assert backend.exists(key)
    assert backend.head(key).size == staged.stat().st_size
    assert backend.head("missing/x.apk") is None
    assert backend.local_path(key) is None


def test_signed_url_v4_with_disposition(tmp_path):
    backend = _backend(_FakeBucket())
    url = backend.signed_url("gcssrc/com.x/5/app.xapk", expires_in=900, filename="app.xapk")
    assert "version=v4" in url
    assert "ttl=900" in url
    assert 'attachment; filename="app.xapk"' in url
    assert "artifacts/gcssrc/com.x/5/app.xapk" in url


def test_metadata_roundtrip_and_reuse(tmp_path):
    bucket = _FakeBucket()
    backend = _backend(bucket)
    store = ArtifactStore(backend, FileVerifier())
    plan = _plan()
    staged = _xapk(tmp_path / "app.xapk")
    key = StorageBackend.object_key(plan, "app.xapk")
    store.commit(plan, staged, key)
    assert backend.get_metadata(backend.metadata_prefix(plan))["key"] == key
    assert store.existing(_plan()) == key
