"""FakeStorageBackend + 对象后端复用路径（local_path 为 None，走 head + 元数据）——为阶段 22 云后端铺垫。"""

import json
import zipfile
from pathlib import Path

from app.domain.models import DownloadPlan
from app.download.artifact_store import ArtifactStore
from app.download.verifier import FileVerifier
from app.storage.base import StorageBackend
from app.storage.fake import FakeStorageBackend


def _plan() -> DownloadPlan:
    return DownloadPlan(package_name="com.x", app_name="X", version_name="1.0", version_code=5, provider="fake", files=[])


def _xapk(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"package_name": "com.x", "version_name": "1.0", "version_code": 5}))
        archive.writestr("base.apk", b"PK\x03\x04base")
    return path


def test_fake_upload_head_signed_url(tmp_path):
    backend = FakeStorageBackend()
    staged = _xapk(tmp_path / "app.xapk")
    key = StorageBackend.object_key(_plan(), "app.xapk")
    backend.upload(staged, key)
    assert backend.exists(key)
    assert backend.head(key).size == staged.stat().st_size
    assert backend.local_path(key) is None  # 对象后端无本地路径
    url = backend.signed_url(key, expires_in=900, filename="app.xapk")
    assert url and key in url and "filename=app.xapk" in url


def test_object_backend_reuse_via_metadata_and_head(tmp_path):
    backend = FakeStorageBackend()
    store = ArtifactStore(backend, FileVerifier())
    plan = _plan()
    staged = _xapk(tmp_path / "app.xapk")
    key = StorageBackend.object_key(plan, "app.xapk")
    store.commit(plan, staged, key)
    # 对象后端：get_metadata 命中 + head 大小一致 → 复用（不回读整产物）
    assert store.existing(_plan()) == key


def test_object_backend_reuse_rejects_size_mismatch(tmp_path):
    backend = FakeStorageBackend()
    store = ArtifactStore(backend, FileVerifier())
    plan = _plan()
    staged = _xapk(tmp_path / "app.xapk")
    key = StorageBackend.object_key(plan, "app.xapk")
    store.commit(plan, staged, key)
    # 篡改对象内容使 head.size 与元数据 size 不一致 → 拒绝复用
    backend.objects[key] = b"truncated"
    assert store.existing(_plan()) is None
