import asyncio
import logging
import os
import socket

from app.catalog.orchestrator import DownloadOrchestrator
from app.catalog.store import CatalogStore
from app.core.config import get_settings
from app.core.logging import configure_logging, log_event
from app.domain.errors import AggregateProviderError
from app.download.downloader import PackageDownloader
from app.download.jobs import DownloadJob, DownloadJobStore
from app.providers.factory import ProviderFactory

logger = logging.getLogger(__name__)


class DownloadWorker:
    def __init__(self, *, worker_id: str | None = None):
        self.settings = get_settings()
        self.settings.ensure_directories()
        store = CatalogStore(self.settings.catalog_db_path)
        self.jobs = DownloadJobStore(store, lease_seconds=self.settings.download_job_lease_seconds)
        self.orchestrator = DownloadOrchestrator(store, ProviderFactory(self.settings), PackageDownloader(self.settings))
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    async def run_forever(self) -> None:
        while True:
            job = self.jobs.claim_next(self.worker_id)
            if job is None:
                await asyncio.sleep(self.settings.download_job_poll_seconds)
                continue
            await self.run_job(job)

    async def run_job(self, job: DownloadJob) -> None:
        log_event(
            logger,
            "download_job_started",
            job_id=job.id,
            request_id=job.request_id,
            package_name=job.request.package_name,
            version_code=job.request.version_code,
            version_name=job.request.version_name,
            provider=job.request.preferred_provider,
        )
        try:
            artifact = await self.orchestrator.download(job.request, request_id=job.request_id)
            self.jobs.mark_succeeded(job.id, artifact)
            log_event(
                logger,
                "download_job_succeeded",
                job_id=job.id,
                request_id=job.request_id,
                package_name=job.request.package_name,
                artifact_path=str(artifact),
            )
        except AggregateProviderError as exc:
            self.jobs.mark_failed(job.id, str(exc), exc.provider_errors)
            log_event(logger, "download_job_failed", job_id=job.id, request_id=job.request_id, message=str(exc))
        except Exception as exc:  # noqa: BLE001 — worker 不能因单个任务退出
            self.jobs.mark_failed(job.id, str(exc))
            log_event(logger, "download_job_failed", job_id=job.id, request_id=job.request_id, message=str(exc))


def main() -> None:
    configure_logging()
    asyncio.run(DownloadWorker().run_forever())


if __name__ == "__main__":
    main()
