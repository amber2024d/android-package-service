import json
import zipfile
from pathlib import Path

from app.domain.models import DownloadPlan
from app.download.artifact_store import ArtifactStore
from app.download.verifier import FileVerifier


def _xapk(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"package_name": "com.x", "version_name": "1.0", "version_code": 5}),
        )
        archive.writestr("base.apk", b"PK\x03\x04dummy-base-apk")
    return path


def _plan() -> DownloadPlan:
    return DownloadPlan(
        package_name="com.x", app_name="X", version_name="1.0", version_code=5, provider="apkpure-signed", files=[]
    )


def _store(tmp_path: Path) -> tuple[ArtifactStore, Path]:
    store = ArtifactStore(tmp_path / "artifacts", FileVerifier())
    plan = _plan()
    artifact_dir = store.plan_dir(plan)
    artifact_dir.mkdir(parents=True)
    artifact = _xapk(artifact_dir / "app.xapk")
    return store, artifact


def _write_metadata(artifact: Path, *, size: int, hashes: dict) -> None:
    (artifact.parent / "metadata.json").write_text(
        json.dumps({"artifact_path": str(artifact), "size": size, "hashes": hashes}),
        encoding="utf-8",
    )


def test_existing_does_not_recompute_full_hash(tmp_path):
    # 复用不重算整文件哈希：故意写入错误哈希，仍应命中（避免每次复用从慢盘重读 150MB）。
    store, artifact = _store(tmp_path)
    _write_metadata(artifact, size=artifact.stat().st_size, hashes={"md5": "deadbeef", "sha1": "bad", "sha256": "bad"})

    assert store.existing(_plan()) == artifact


def test_existing_still_rejects_size_mismatch(tmp_path):
    # 大小校验仍生效（stat，便宜）：文件被截断/替换则拒绝复用。
    store, artifact = _store(tmp_path)
    _write_metadata(artifact, size=artifact.stat().st_size + 999, hashes={})

    assert store.existing(_plan()) is None


def test_existing_rejects_wrong_version_manifest(tmp_path):
    # manifest 版本与请求不符则拒绝复用（读 zip 中央目录，便宜）。
    store, artifact = _store(tmp_path)
    _write_metadata(artifact, size=artifact.stat().st_size, hashes={})
    mismatched = _plan().model_copy(update={"version_code": 999})

    assert store.existing(mismatched) is None
