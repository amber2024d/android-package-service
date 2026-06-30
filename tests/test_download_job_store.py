import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.catalog.store import CatalogStore
from app.domain.models import AndroidPackageRequest
from app.download.jobs import FAILED, RUNNING, SUCCEEDED, DownloadJobStore


def test_enqueue_reuses_active_job(tmp_path):
    jobs = DownloadJobStore(CatalogStore(tmp_path / "catalog.sqlite"))
    request = AndroidPackageRequest(package_name="p", version_code=1, preferred_provider="fake")

    first = jobs.enqueue(request, "r1")
    second = jobs.enqueue(request, "r2")

    assert second.id == first.id


def test_failed_job_can_be_requeued(tmp_path):
    jobs = DownloadJobStore(CatalogStore(tmp_path / "catalog.sqlite"))
    request = AndroidPackageRequest(package_name="p", version_code=1, preferred_provider="fake")

    first = jobs.enqueue(request, "r1")
    jobs.mark_failed(first.id, "boom")
    second = jobs.enqueue(request, "r2")

    assert jobs.get(first.id).status == FAILED
    assert second.id != first.id


def test_expired_running_job_can_be_reclaimed(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite")
    jobs = DownloadJobStore(store, lease_seconds=60)
    job = jobs.enqueue(AndroidPackageRequest(package_name="p"), "r1")
    claimed = jobs.claim_next("worker-a")
    assert claimed.id == job.id

    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with store.connect() as conn:
        conn.execute("UPDATE download_jobs SET lease_expires = ? WHERE id = ?", (expired, job.id))

    reclaimed = jobs.claim_next("worker-b")
    assert reclaimed.id == job.id
    assert reclaimed.status == RUNNING


def test_mark_succeeded_records_hit_provider(tmp_path):
    jobs = DownloadJobStore(CatalogStore(tmp_path / "catalog.sqlite"))
    job = jobs.enqueue(AndroidPackageRequest(package_name="p"), "r1")
    jobs.claim_next("worker-a")

    jobs.mark_succeeded(job.id, Path("/nas/artifacts/google-play/p/100/app.apk"), "google-play")

    stored = jobs.get(job.id)
    assert stored.status == SUCCEEDED
    assert stored.succeeded_provider == "google-play"
    assert str(stored.artifact_path) == "/nas/artifacts/google-play/p/100/app.apk"


def test_mark_succeeded_without_provider_leaves_column_null(tmp_path):
    # 向后兼容：不传 provider 时该列为 NULL，监控回退按 artifact_path 解析。
    jobs = DownloadJobStore(CatalogStore(tmp_path / "catalog.sqlite"))
    job = jobs.enqueue(AndroidPackageRequest(package_name="p"), "r1")
    jobs.claim_next("worker-a")

    jobs.mark_succeeded(job.id, Path("/nas/artifacts/aptoide/p/1/app.apk"))

    assert jobs.get(job.id).succeeded_provider is None


def test_migrates_old_download_jobs_adds_succeeded_provider(tmp_path):
    # 监控落库前建的旧库 download_jobs 没有 succeeded_provider 列；CatalogStore 初始化应幂等补列，老任务仍可读。
    db = tmp_path / "catalog.sqlite"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE download_jobs ("
            "id TEXT PRIMARY KEY, request_key TEXT NOT NULL, package TEXT NOT NULL, "
            "version_code INTEGER, version_name TEXT, provider TEXT, status TEXT NOT NULL, "
            "artifact_path TEXT, error TEXT, provider_errors TEXT, request_id TEXT, worker TEXT, "
            "lease_expires TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)"
        )
        conn.execute(
            "INSERT INTO download_jobs (id, request_key, package, status, artifact_path, created_at, updated_at) "
            "VALUES ('old', 'k', 'p', 'succeeded', '/nas/artifacts/apkpure-signed/p/1/app.apk', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()

    jobs = DownloadJobStore(CatalogStore(db))  # 初始化触发 _migrate 补列

    with closing(jobs.store.connect()) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(download_jobs)")}
    assert "succeeded_provider" in columns
    old = jobs.get("old")
    assert old.status == SUCCEEDED
    assert old.succeeded_provider is None  # 老任务该列回退为 NULL


def test_concurrent_claims_take_distinct_jobs(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite")
    jobs = DownloadJobStore(store, lease_seconds=60)
    first = jobs.enqueue(AndroidPackageRequest(package_name="p1"), "r1")
    second = jobs.enqueue(AndroidPackageRequest(package_name="p2"), "r2")

    claimed_a = jobs.claim_next("worker:1")
    claimed_b = jobs.claim_next("worker:2")

    assert claimed_a is not None
    assert claimed_b is not None
    assert {claimed_a.id, claimed_b.id} == {first.id, second.id}
    with closing(store.connect()) as conn:
        rows = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT id, worker FROM download_jobs WHERE id IN (?, ?)",
                (first.id, second.id),
            )
        }
    assert set(rows.values()) == {"worker:1", "worker:2"}
