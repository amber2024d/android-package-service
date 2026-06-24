import json
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


def test_health(tmp_path):
    client = _client(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}


def test_fake_provider_fallback(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fdroid.fdroid")
    assert response.status_code == 200
    body = response.json()
    assert body["packageName"] == "org.fdroid.fdroid"
    assert body["provider"] == "fake"


def test_fake_provider_forced(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fdroid.fdroid?provider=fake")
    assert response.status_code == 200
    assert response.json()["provider"] == "fake"


def test_download_single_apk(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.android.package-archive"
    assert response.content[:2] == b"PK"


def test_download_split_xapk(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/com.oakever.arrows/download?provider=fake")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    artifact = tmp_path / "artifact.xapk"
    artifact.write_bytes(response.content)
    with ZipFile(artifact) as zip_file:
        names = set(zip_file.namelist())
        manifest = json.loads(zip_file.read("manifest.json"))
    assert {"manifest.json", "base.apk", "config.arm64_v8a.apk"} <= names
    assert manifest["package_name"] == "com.oakever.arrows"


def test_download_apks_keeps_extension(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fake.apks/download?provider=fake")
    assert response.status_code == 200
    assert 'filename="org.fake.apks_2.0.0_200_fake.apks"' in response.headers["content-disposition"]


def test_hash_mismatch_returns_verify_failed(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fake.bad-hash/download?provider=fake")
    assert response.status_code == 502
    assert response.json()["error"] == "VERIFY_FAILED"


def _client(tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.provider_apkpure_signed_enabled = False
    settings.provider_google_play_enabled = False
    settings.provider_aptoide_enabled = False
    settings.provider_apkpure_proto_enabled = False
    settings.provider_apkpure_web_enabled = False
    settings.ensure_directories()
    return TestClient(app)
