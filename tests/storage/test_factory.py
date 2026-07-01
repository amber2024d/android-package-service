import pytest

from app.core.config import Settings
from app.storage.factory import build_storage_backend
from app.storage.local import LocalStorageBackend


def test_local_backend_roots_at_artifacts_dir(tmp_path):
    settings = Settings(storage_backend="local", nas_mount_path=tmp_path / "nas")
    backend = build_storage_backend(settings)
    assert isinstance(backend, LocalStorageBackend)
    assert backend.root == settings.artifacts_dir


def test_gcs_requires_bucket():
    with pytest.raises(RuntimeError):
        build_storage_backend(Settings(storage_backend="gcs"))  # 缺 GCS_BUCKET → 清晰报错


def test_s3_requires_bucket():
    with pytest.raises(RuntimeError):
        build_storage_backend(Settings(storage_backend="s3"))  # 缺 S3_BUCKET → 清晰报错


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        build_storage_backend(Settings(storage_backend="weird"))
