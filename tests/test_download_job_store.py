from datetime import UTC, datetime, timedelta

from app.catalog.store import CatalogStore
from app.domain.models import AndroidPackageRequest
from app.download.jobs import FAILED, RUNNING, DownloadJobStore


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
