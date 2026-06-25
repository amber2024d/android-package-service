import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.routes import get_catalog
from app.catalog.catalog import VersionCatalog
from app.catalog.collectors.base import Collector, VersionRecord
from app.catalog.store import CatalogStore
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
    settings.ensure_directories()
    return TestClient(app), settings.catalog_db_path


def _seed(db: Path, *, package: str, versions, collected: bool) -> None:
    CatalogStore(db)  # 确保 schema
    with closing(sqlite3.connect(db)) as conn:
        for name, code, downloadable in versions:
            conn.execute(
                "INSERT INTO versions(package, version_name, version_code, downloadable) VALUES(?,?,?,?)",
                (package, name, code, downloadable),
            )
        if collected:
            conn.execute(
                "INSERT INTO collection_state(package, last_full_at, last_refresh_at) VALUES(?,?,?)",
                (package, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        conn.commit()


class _FakeCollector(Collector):
    def __init__(self, source: str, records: list[VersionRecord]):
        self.source = source
        self._records = records
        self.calls = 0

    async def collect(self, package: str) -> list[VersionRecord]:
        self.calls += 1
        return list(self._records)


class _SpyCatalog:
    """鸭子类型的 catalog：同步记录 ensure_collected 触发，不做真实收集（隔离 fire-and-forget 时序）。"""

    def __init__(self):
        self.collected: list[tuple[str, bool]] = []

    def ensure_collected(self, package: str, *, need_history: bool = True):
        self.collected.append((package, need_history))

        async def _noop():
            return None

        return _noop()

    def list_downloadable(self, package: str):
        return []


def test_versions_lists_downloadable_only_and_sorted(tmp_path):
    client, db = _client(tmp_path)
    _seed(
        db,
        package="com.x",
        versions=[("3.0.0", 1772, 1), ("2.9.0", 33, 1), ("1.0.0", None, 0)],  # 1.0.0 是 known-only
        collected=True,
    )
    resp = client.get("/api/v1/android/apps/com.x/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["packageName"] == "com.x"
    assert body["versions"] == [
        {"versionName": "3.0.0", "versionCode": 1772},
        {"versionName": "2.9.0", "versionCode": 33},
    ]  # 只出 downloadable，按版本号降序；known-only 不出


def test_versions_first_collect_blocks_then_reads_db(tmp_path):
    client, db = _client(tmp_path)
    collector = _FakeCollector("apkpure", [VersionRecord("2.0.0", 2), VersionRecord("1.0.0", 1)])
    catalog = VersionCatalog(CatalogStore(db), [collector], now_fn=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_catalog] = lambda: catalog
    try:
        first = client.get("/api/v1/android/apps/com.new/versions")
        assert [v["versionName"] for v in first.json()["versions"]] == ["2.0.0", "1.0.0"]
        assert collector.calls == 1  # 首采阻塞一次
        second = client.get("/api/v1/android/apps/com.new/versions")
        assert [v["versionName"] for v in second.json()["versions"]] == ["2.0.0", "1.0.0"]
        assert collector.calls == 1  # 复查直接读库
    finally:
        app.dependency_overrides.clear()


def test_versions_empty_when_nothing_collected(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("/api/v1/android/apps/com.unknown/versions")
    assert resp.status_code == 200
    assert resp.json() == {"packageName": "com.unknown", "versions": []}


def test_download_with_version_triggers_background_collect(tmp_path):
    client, _ = _client(tmp_path)
    spy = _SpyCatalog()
    app.dependency_overrides[get_catalog] = lambda: spy
    try:
        resp = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake&versionCode=100")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()
    assert spy.collected == [("org.fdroid.fdroid", True)]


def test_download_latest_does_not_trigger_collect(tmp_path):
    client, _ = _client(tmp_path)
    spy = _SpyCatalog()
    app.dependency_overrides[get_catalog] = lambda: spy
    try:
        resp = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()
    assert spy.collected == []  # 最新版快路径不触发收集


def test_download_same_version_reuses_artifact(tmp_path, caplog):
    client, _ = _client(tmp_path)
    spy = _SpyCatalog()  # 隔离后台收集，专测下载复用
    app.dependency_overrides[get_catalog] = lambda: spy
    try:
        client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake&versionCode=100")
        caplog.clear()
        with caplog.at_level(logging.INFO):
            resp = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake&versionCode=100")
        assert resp.status_code == 200
        assert '"event": "artifact_reused"' in caplog.text
    finally:
        app.dependency_overrides.clear()
