"""端到端验证对象后端下发（阶段 22）：/download 命中后返回 302 到 signed URL。

用签名的 FakeStorageBackend 替换工厂（local_path=None → 走 signed URL 302），无需真实云。
"""

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.storage.fake import FakeStorageBackend


def test_object_backend_download_redirects_to_signed_url(tmp_path, monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.download_async_enabled = False
    settings.signed_url_ttl_seconds = 1234
    settings.ensure_directories()

    shared = FakeStorageBackend()  # 签名后端；下载器与下发共用同一实例，上传的产物下发时可签名
    monkeypatch.setattr("app.download.downloader.build_storage_backend", lambda _s: shared)
    monkeypatch.setattr("app.api.routes.build_storage_backend", lambda _s: shared)

    client = TestClient(app)
    try:
        resp = client.get(
            "/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake",
            follow_redirects=False,
        )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://fake-storage.local/")
    assert "expires=1234" in location
    assert "filename=" in location
    # 产物确实上传到了对象后端
    assert len(shared.objects) == 1
