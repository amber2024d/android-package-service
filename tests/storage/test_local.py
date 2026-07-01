import json
import zipfile
from pathlib import Path

from app.domain.models import DownloadPlan
from app.download.artifact_store import ArtifactStore
from app.download.verifier import FileVerifier
from app.storage.base import StorageBackend
from app.storage.local import LocalStorageBackend


def _xapk(path: Path, *, version_code: int = 5) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"package_name": "com.x", "version_name": "1.0", "version_code": version_code}),
        )
        archive.writestr("base.apk", b"PK\x03\x04dummy-base-apk")
    return path


def _plan() -> DownloadPlan:
    return DownloadPlan(
        package_name="com.x", app_name="X", version_name="1.0", version_code=5, provider="apkpure-signed", files=[]
    )


def _store(tmp_path: Path, *, version_code: int = 5):
    root = tmp_path / "artifacts"
    backend = LocalStorageBackend(root)
    store = ArtifactStore(backend, FileVerifier())
    plan = _plan()
    key = StorageBackend.object_key(plan, "app.xapk")
    artifact = root / key
    artifact.parent.mkdir(parents=True)
    _xapk(artifact, version_code=version_code)
    return store, backend, plan, key, artifact


def _write_meta(backend: LocalStorageBackend, plan: DownloadPlan, key: str, *, size: int, hashes: dict | None = None) -> None:
    backend.put_metadata(backend.metadata_prefix(plan), {"key": key, "size": size, "hashes": hashes or {}})


def test_object_key_layout(tmp_path):
    key = StorageBackend.object_key(_plan(), "app.xapk")
    assert key == "apkpure-signed/com.x/5/app.xapk"


def test_existing_does_not_recompute_full_hash(tmp_path):
    # 复用不重算整文件哈希：故意写错误哈希，仍应命中（避免每次复用从慢盘重读大包）。
    store, backend, plan, key, artifact = _store(tmp_path)
    _write_meta(backend, plan, key, size=artifact.stat().st_size, hashes={"md5": "deadbeef", "sha1": "bad", "sha256": "bad"})
    assert store.existing(_plan()) == key


def test_existing_rejects_size_mismatch(tmp_path):
    # 大小校验仍生效（stat，便宜）：文件被截断/替换则拒绝复用。
    store, backend, plan, key, artifact = _store(tmp_path)
    _write_meta(backend, plan, key, size=artifact.stat().st_size + 999)
    assert store.existing(_plan()) is None


def test_existing_rejects_wrong_version_manifest(tmp_path):
    # 元数据存在、但产物 manifest 版本与请求不符 → 读 zip 中央目录核出、拒绝复用。
    store, backend, plan, key, artifact = _store(tmp_path, version_code=6)
    _write_meta(backend, plan, key, size=artifact.stat().st_size)
    assert store.existing(_plan()) is None  # plan 请求 5，产物 manifest 是 6


def test_existing_none_when_no_metadata(tmp_path):
    store, backend, plan, key, artifact = _store(tmp_path)
    assert store.existing(_plan()) is None  # 未写元数据边车


def test_commit_uploads_and_writes_metadata(tmp_path):
    root = tmp_path / "artifacts"
    backend = LocalStorageBackend(root)
    store = ArtifactStore(backend, FileVerifier())
    plan = _plan()
    staged = _xapk(tmp_path / "staged.xapk")
    key = StorageBackend.object_key(plan, "app.xapk")
    store.commit(plan, staged, key)
    # 上传到 artifacts_dir/key，元数据边车含 key/size/hashes
    assert (root / key).exists()
    assert backend.head(key).size == staged.stat().st_size
    meta = backend.get_metadata(backend.metadata_prefix(plan))
    assert meta["key"] == key and meta["size"] == staged.stat().st_size and meta["hashes"]
    # commit 后可复用
    assert store.existing(_plan()) == key


def test_signed_url_is_none_for_local(tmp_path):
    backend = LocalStorageBackend(tmp_path / "artifacts")
    assert backend.signed_url("a/b/c/x.apk", expires_in=60, filename="x.apk") is None
