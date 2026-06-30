from contextlib import closing
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.api.monitor import MonitorService
from app.catalog.store import CatalogStore
from app.core.config import get_settings
from app.main import app

NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)


def _store(tmp_path) -> CatalogStore:
    return CatalogStore(tmp_path / "catalog.sqlite")


def _service(store: CatalogStore, tmp_path) -> MonitorService:
    return MonitorService(store, artifacts_dir=tmp_path / "nas" / "artifacts", now_fn=lambda: NOW)


def _insert_job(store: CatalogStore, **kw) -> None:
    cols = (
        "id", "request_key", "package", "version_code", "version_name", "provider",
        "status", "artifact_path", "succeeded_provider", "error", "provider_errors", "worker",
        "created_at", "updated_at", "started_at", "finished_at",
    )
    row = {c: kw.get(c) for c in cols}
    row["request_key"] = row["request_key"] or row["id"]
    row["created_at"] = row["created_at"] or NOW.isoformat()
    row["updated_at"] = row["updated_at"] or row["created_at"]
    with closing(store.connect()) as conn:
        conn.execute(
            f"INSERT INTO download_jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(row[c] for c in cols),
        )


def _iso(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def _artifact(tmp_path, provider: str) -> str:
    return str(tmp_path / "nas" / "artifacts" / provider / "com.x" / "100" / "app.apk")


def _seed(store: CatalogStore, tmp_path) -> None:
    # versions / sources / ledger（收录规模）
    with closing(store.connect()) as conn:
        conn.executemany(
            "INSERT INTO versions (package, version_name, version_code, downloadable) VALUES (?, ?, ?, ?)",
            [
                ("com.a", "1.0", 100, 1),
                ("com.a", "1.1", 110, 1),
                ("com.b", "2.0", 200, 0),
            ],
        )
        conn.executemany(
            "INSERT INTO version_sources (package, version_name, source) VALUES (?, ?, ?)",
            [("com.a", "1.0", "apkpure"), ("com.a", "1.1", "apkpure"), ("com.b", "2.0", "appmagic")],
        )
        conn.execute(
            "INSERT INTO ledger (package, version_name, version_code) VALUES (?, ?, ?)",
            ("com.a", "1.0", 100),
        )

    # 今天成功：apkpure-signed 命中，下载耗时 10s
    _insert_job(
        store, id="s1", package="com.a", version_code=100, status="succeeded",
        artifact_path=_artifact(tmp_path, "apkpure-signed"),
        started_at=_iso(seconds=20), finished_at=_iso(seconds=10),
    )
    # 今天失败：apkpure-signed NOT_FOUND → google-play NETWORK_ERROR
    _insert_job(
        store, id="f1", package="com.a", version_code=110, status="failed",
        error="All providers failed.",
        provider_errors='[{"provider":"apkpure-signed","error":"NOT_FOUND","message":"no"},'
        '{"provider":"google-play","error":"NETWORK_ERROR","message":"timeout"}]',
        started_at=_iso(seconds=30), finished_at=_iso(seconds=5),
    )
    # 进行中
    _insert_job(store, id="r1", package="com.b", status="running", started_at=_iso(seconds=40))
    # 排队
    _insert_job(store, id="q1", package="com.c", status="queued", created_at=_iso(seconds=3))
    # 窗口外（10 天前）成功，不计入近 7 天分析与 provider 流转
    _insert_job(
        store, id="old", package="com.a", version_code=100, status="succeeded",
        artifact_path=_artifact(tmp_path, "aptoide"),
        created_at=_iso(days=10), started_at=_iso(days=10), finished_at=_iso(days=10),
    )


def test_overview_counts(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    o = _service(store, tmp_path).snapshot(days=7)["overview"]

    assert o["packages"] == 2  # com.a, com.b in versions
    assert o["versions"] == {"total": 3, "downloadable": 2, "knownOnly": 1}
    assert o["ledgerFacts"] == 1
    assert {s["source"] for s in o["sources"]} == {"apkpure", "appmagic"}
    assert o["jobs"]["succeeded"] == 2  # today + old
    assert o["jobs"]["failed"] == 1
    assert o["jobs"]["running"] == 1
    assert o["jobs"]["queued"] == 1
    assert o["jobs"]["total"] == 5


def test_tasks_buckets_and_elapsed(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    t = _service(store, tmp_path).snapshot(days=7)["tasks"]

    assert [j["id"] for j in t["running"]] == ["r1"]
    assert t["running"][0]["elapsedMs"] == 40000  # now - started (40s)
    assert [j["id"] for j in t["queued"]] == ["q1"]
    assert t["queued"][0]["elapsedMs"] == 3000
    assert [j["id"] for j in t["recentFailed"]] == ["f1"]
    assert t["recentFailed"][0]["providerErrors"][0]["provider"] == "apkpure-signed"
    succeeded_ids = {j["id"] for j in t["recentSucceeded"]}
    assert {"s1", "old"} <= succeeded_ids
    s1 = next(j for j in t["recentSucceeded"] if j["id"] == "s1")
    assert s1["succeededProvider"] == "apkpure-signed"
    assert s1["downloadMs"] == 10000


def test_providers_flow_within_window(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    items = {i["provider"]: i for i in _service(store, tmp_path).snapshot(days=7)["providers"]["items"]}

    assert items["apkpure-signed"]["successes"] == 1
    assert items["apkpure-signed"]["failures"] == 1
    assert items["apkpure-signed"]["errors"] == {"NOT_FOUND": 1}
    assert items["google-play"]["failures"] == 1
    assert items["google-play"]["errors"] == {"NETWORK_ERROR": 1}
    # 窗口外的 aptoide 成功不计入
    assert "aptoide" not in items


def test_succeeded_provider_column_preferred_over_artifact(tmp_path):
    # succeeded_provider 落库为 google-play，但 artifact 落在 apkpure-signed 目录：命中来源以列为准，路径解析不参与。
    store = _store(tmp_path)
    _insert_job(
        store, id="col", package="com.a", version_code=100, status="succeeded",
        succeeded_provider="google-play",
        artifact_path=_artifact(tmp_path, "apkpure-signed"),
        started_at=_iso(seconds=20), finished_at=_iso(seconds=10),
    )
    snap = _service(store, tmp_path).snapshot(days=7)

    job = next(j for j in snap["tasks"]["recentSucceeded"] if j["id"] == "col")
    assert job["succeededProvider"] == "google-play"
    items = {i["provider"]: i for i in snap["providers"]["items"]}
    assert items["google-play"]["successes"] == 1
    assert "apkpure-signed" not in items


def test_succeeded_provider_falls_back_to_artifact_when_null(tmp_path):
    # 老库（succeeded_provider 为 NULL）：回退按 artifact 路径段还原命中来源。
    store = _store(tmp_path)
    _insert_job(
        store, id="legacy", package="com.a", version_code=100, status="succeeded",
        artifact_path=_artifact(tmp_path, "aptoide"),
        started_at=_iso(seconds=20), finished_at=_iso(seconds=10),
    )
    snap = _service(store, tmp_path).snapshot(days=7)

    job = next(j for j in snap["tasks"]["recentSucceeded"] if j["id"] == "legacy")
    assert job["succeededProvider"] == "aptoide"
    items = {i["provider"]: i for i in snap["providers"]["items"]}
    assert items["aptoide"]["successes"] == 1


def test_analytics_totals_duration_daily(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    a = _service(store, tmp_path).snapshot(days=7)["analytics"]

    assert a["totals"] == {"total": 2, "succeeded": 1, "failed": 1, "successRate": 0.5}
    assert a["duration"]["count"] == 1
    assert a["duration"]["avgMs"] == 10000.0
    assert a["duration"]["p95Ms"] == 10000.0
    assert len(a["daily"]) == 7
    assert a["daily"][-1] == {"date": "2026-06-29", "total": 2, "succeeded": 1, "failed": 1}
    # 不变量：日柱加总恒等于窗口总数（窗口下界与分桶范围同口径）
    assert sum(d["total"] for d in a["daily"]) == a["totals"]["total"]
    per = {p["package"]: p for p in a["perPackage"]}
    assert per["com.a"]["total"] == 2
    assert per["com.a"]["succeeded"] == 1
    assert per["com.a"]["successRate"] == 0.5
    assert per["com.a"]["avgMs"] == 10000.0


def test_window_day_param_narrows(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    a = _service(store, tmp_path).snapshot(days=1)["analytics"]
    assert len(a["daily"]) == 1
    assert a["totals"]["total"] == 2  # 窗口外的 old 仍不计入


def test_window_edge_consistency(tmp_path):
    # 06-23T13:00（now=06-29T12:00、days=7）：旧滚动窗下界 now-7d=06-22T12:00 会把它纳入 totals，
    # 但落在自然日桶范围 [06-23..06-29] 之外，造成 sum(daily) < totals。新口径把下界对齐到 06-23T00:00，
    # 二者一致——这条边界任务仍在窗口内、且能落进 06-23 桶。
    store = _store(tmp_path)
    _insert_job(
        store, id="edge", package="com.a", status="succeeded",
        artifact_path=_artifact(tmp_path, "apkpure-signed"),
        created_at=_iso(days=5, hours=23), started_at=_iso(days=5, hours=23), finished_at=_iso(days=5, hours=23),
    )
    a = _service(store, tmp_path).snapshot(days=7)["analytics"]
    assert a["totals"]["total"] == 1
    assert a["daily"][0]["date"] == "2026-06-23"
    assert sum(d["total"] for d in a["daily"]) == a["totals"]["total"] == 1


def test_job_before_calendar_window_excluded(tmp_path):
    # 06-22T13:00 落在自然日窗口（06-23 起）之前：新口径从 totals 与 daily 同时排除，保持一致。
    store = _store(tmp_path)
    _insert_job(
        store, id="before", package="com.a", status="succeeded",
        artifact_path=_artifact(tmp_path, "apkpure-signed"),
        created_at=_iso(days=6, hours=23), started_at=_iso(days=6, hours=23), finished_at=_iso(days=6, hours=23),
    )
    a = _service(store, tmp_path).snapshot(days=7)["analytics"]
    assert a["totals"]["total"] == 0
    assert sum(d["total"] for d in a["daily"]) == 0


def test_empty_database_is_safe(tmp_path):
    store = _store(tmp_path)
    snap = _service(store, tmp_path).snapshot(days=7)
    assert snap["overview"]["packages"] == 0
    assert snap["overview"]["jobs"]["total"] == 0
    assert snap["analytics"]["totals"]["successRate"] is None
    assert snap["analytics"]["duration"]["count"] == 0
    assert snap["tasks"]["running"] == []
    assert len(snap["analytics"]["daily"]) == 7


def test_provider_from_relocated_artifact_path(tmp_path):
    # artifacts_dir 与实际落盘前缀不同（如容器内 /mnt/nas）时，回退按 "artifacts" 段解析
    svc = MonitorService(_store(tmp_path), artifacts_dir=tmp_path / "nas" / "artifacts", now_fn=lambda: NOW)
    assert svc._provider_from_artifact("/mnt/nas/apks/artifacts/google-play/com.x/100/app.apk") == "google-play"
    assert svc._provider_from_artifact(None) is None


def test_snapshot_endpoint(tmp_path):
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.ensure_directories()
    # 经 CatalogStore 建表后注入任务数据，再请求端点
    store = CatalogStore(settings.catalog_db_path)
    _insert_job(store, id="s1", package="com.a", status="succeeded",
                artifact_path=str(settings.artifacts_dir / "fake" / "com.a" / "100" / "app.apk"),
                started_at=_iso(seconds=20), finished_at=_iso(seconds=10))
    try:
        client = TestClient(app)
        res = client.get("/api/v1/monitor/snapshot?days=7")
        assert res.status_code == 200
        body = res.json()
        assert set(body) == {"generatedAt", "windowDays", "overview", "tasks", "providers", "analytics"}
        assert body["windowDays"] == 7
        assert body["overview"]["jobs"]["succeeded"] == 1
        dash = client.get("/dashboard")
        assert dash.status_code == 200
        assert "监控指挥台" in dash.text
        assert "/dashboard/echarts.min.js" in dash.text
        js = client.get("/dashboard/echarts.min.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]
    finally:
        get_settings.cache_clear()
