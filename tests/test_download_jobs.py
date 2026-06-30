import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.download.worker import DownloadWorker
from app.main import app


def test_download_queues_job_and_worker_serves_file(tmp_path):
    client = _client(tmp_path)

    first = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake")
    assert first.status_code == 202
    body = first.json()
    assert body["status"] == "queued"
    assert body["fileUrl"] is None

    second = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake")
    assert second.status_code == 202
    assert second.json()["jobId"] == body["jobId"]

    worker = DownloadWorker(worker_id="test")
    job = worker.jobs.claim_next("test")
    assert job is not None
    asyncio.run(worker.run_job(job))
    # 命中 provider 端到端落库（真实编排器返回 plan.provider）
    assert worker.jobs.get(job.id).succeeded_provider == "fake"

    status = client.get(body["statusUrl"].replace("http://testserver", ""))
    assert status.status_code == 200
    done = status.json()
    assert done["status"] == "succeeded"
    assert done["fileUrl"].endswith(f"/api/v1/android/downloads/{body['jobId']}/file")

    file_resp = client.get(done["fileUrl"].replace("http://testserver", ""))
    assert file_resp.status_code == 200
    assert file_resp.content[:2] == b"PK"


def test_async_download_rejects_disabled_provider_before_queue(tmp_path):
    client = _client(tmp_path)

    response = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=google-play")

    assert response.status_code == 400
    assert response.json()["error"] == "UNSUPPORTED"


def _client(tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.provider_fake_enabled = True
    settings.provider_fake_failing_enabled = False
    settings.provider_apkpure_signed_enabled = False
    settings.provider_google_play_enabled = False
    settings.provider_aptoide_enabled = False
    settings.provider_apkpure_proto_enabled = False
    settings.provider_apkpure_web_enabled = False
    settings.provider_apkmirror_enabled = False
    settings.appmagic_enabled = False
    settings.download_async_enabled = True
    settings.ensure_directories()
    return TestClient(app)
