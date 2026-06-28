import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.provider_fake_enabled = True
    settings.provider_fake_failing_enabled = True
    settings.provider_apkpure_signed_enabled = False
    settings.provider_google_play_enabled = False
    settings.provider_aptoide_enabled = False
    settings.provider_apkpure_proto_enabled = False
    settings.provider_apkpure_web_enabled = False
    settings.download_async_enabled = False
    settings.ensure_directories()
    return TestClient(app), settings.catalog_db_path


def _ledger_rows(db_path: Path):
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT package, version_name, version_code, source FROM ledger ORDER BY version_code"
        ).fetchall()


def test_download_backfills_ledger(tmp_path):
    client, db = _client(tmp_path)
    try:
        # split→xapk：打包出的 manifest.json 带权威 (com.oakever.arrows, 1.18.0, 43)
        response = client.get("/api/v1/android/apps/com.oakever.arrows/download?provider=fake")
        assert response.status_code == 200
        assert _ledger_rows(db) == [("com.oakever.arrows", "1.18.0", 43, "fake")]
    finally:
        get_settings.cache_clear()


def test_backfill_is_idempotent_across_redownload(tmp_path):
    client, db = _client(tmp_path)
    try:
        client.get("/api/v1/android/apps/com.oakever.arrows/download?provider=fake")
        client.get("/api/v1/android/apps/com.oakever.arrows/download?provider=fake")
        assert _ledger_rows(db) == [("com.oakever.arrows", "1.18.0", 43, "fake")]
    finally:
        get_settings.cache_clear()


def test_unparsable_artifact_downloads_without_ledger_row(tmp_path):
    client, db = _client(tmp_path)
    try:
        # fake 的 fdroid 产物是只含 classes.dex 的假 apk —— 无法解析，跳过回填但下载成功
        response = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake")
        assert response.status_code == 200
        assert _ledger_rows(db) == []
    finally:
        get_settings.cache_clear()


def test_backfill_failure_does_not_break_download(tmp_path, monkeypatch):
    client, db = _client(tmp_path)

    def boom(_artifact):
        raise RuntimeError("manifest parser exploded")

    monkeypatch.setattr("app.download.downloader.parse_artifact", boom)
    try:
        response = client.get("/api/v1/android/apps/com.oakever.arrows/download?provider=fake")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert _ledger_rows(db) == []  # 回填失败被隔离，账本无行
    finally:
        get_settings.cache_clear()
