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
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    async def run_forever(self) -> None:
        concurrency = self.settings.download_worker_concurrency
        log_event(logger, "download_worker_started", worker_id=self.worker_id, concurrency=concurrency)
        tasks = [
            asyncio.create_task(self._run_slot(index), name=f"download-worker-{index}")
            for index in range(1, concurrency + 1)
        ]
        await asyncio.gather(*tasks)

    async def _run_slot(self, slot: int) -> None:
        worker_id = f"{self.worker_id}:{slot}"
        orchestrator = self._new_orchestrator()
        while True:
            job = self.jobs.claim_next(worker_id)
            if job is None:
                await asyncio.sleep(self.settings.download_job_poll_seconds)
                continue
            await self.run_job(job, orchestrator=orchestrator, worker_id=worker_id)

    def _new_orchestrator(self) -> DownloadOrchestrator:
        store = CatalogStore(self.settings.catalog_db_path)
        return DownloadOrchestrator(store, ProviderFactory(self.settings), PackageDownloader(self.settings))

    async def run_job(
        self,
        job: DownloadJob,
        *,
        orchestrator: DownloadOrchestrator | None = None,
        worker_id: str | None = None,
    ) -> None:
        worker_id = worker_id or self.worker_id
        orchestrator = orchestrator or self._new_orchestrator()
        log_event(
            logger,
            "download_job_started",
            job_id=job.id,
            worker_id=worker_id,
            request_id=job.request_id,
            package_name=job.request.package_name,
            version_code=job.request.version_code,
            version_name=job.request.version_name,
            provider=job.request.preferred_provider,
        )
        try:
            artifact = await orchestrator.download(job.request, request_id=job.request_id)
            self.jobs.mark_succeeded(job.id, artifact)
            log_event(
                logger,
                "download_job_succeeded",
                job_id=job.id,
                worker_id=worker_id,
                request_id=job.request_id,
                package_name=job.request.package_name,
                artifact_path=str(artifact),
            )
        except AggregateProviderError as exc:
            self.jobs.mark_failed(job.id, str(exc), exc.provider_errors)
            log_event(
                logger,
                "download_job_failed",
                job_id=job.id,
                worker_id=worker_id,
                request_id=job.request_id,
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — worker 不能因单个任务退出
            self.jobs.mark_failed(job.id, str(exc))
            log_event(
                logger,
                "download_job_failed",
                job_id=job.id,
                worker_id=worker_id,
                request_id=job.request_id,
                message=str(exc),
            )


def main() -> None:
    configure_logging()
    asyncio.run(DownloadWorker().run_forever())


if __name__ == "__main__":
    main()
