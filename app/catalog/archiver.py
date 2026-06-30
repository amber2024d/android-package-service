import asyncio
import logging

from app.catalog.orchestrator import DownloadOrchestrator
from app.catalog.store import CatalogStore
from app.core.config import Settings
from app.core.logging import log_event
from app.domain.models import AndroidPackageRequest
from app.download.downloader import PackageDownloader
from app.providers.factory import ProviderFactory

logger = logging.getLogger(__name__)


class CatalogArchiver:
    """主动归档（§11.1）：把目录增量发现的新版本当即下载入 NAS 档案馆。

    作为 `VersionCatalog.on_new_versions` 钩子接入——只在**增量**轮被调用（首次全量不回溯整窗，见 catalog）。
    经现有编排器下载（幂等：artifact 已存即复用、与用户下载共用），低并发限流、有限重试、失败隔离，
    成功自然走阶段 10 回填钩子写账本。`orchestrator` 可注入（测试），否则按 settings 懒构建（仅真有新版本时）。
    """

    def __init__(self, settings: Settings, *, orchestrator: DownloadOrchestrator | None = None):
        self.settings = settings
        self.enabled = settings.archive_enabled
        self.concurrency = max(1, settings.archive_concurrency)
        self.max_retries = max(0, settings.archive_max_retries)
        self._orchestrator = orchestrator

    async def archive_new(self, package: str, versions: list[tuple[str, int | None]]) -> None:
        if not self.enabled or not versions:
            return
        orchestrator = self._build_orchestrator()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def archive(version_name: str, version_code: int | None) -> None:
            async with semaphore:
                await self._archive_one(orchestrator, package, version_name, version_code)

        await asyncio.gather(*(archive(name, code) for name, code in versions))

    async def _archive_one(
        self, orchestrator: DownloadOrchestrator, package: str, version_name: str, version_code: int | None
    ) -> None:
        request = AndroidPackageRequest(package_name=package, version_name=version_name, version_code=version_code)
        request_id = f"archive:{package}:{version_name}"
        for attempt in range(self.max_retries + 1):
            try:
                artifact, _ = await orchestrator.download(request, request_id=request_id)
                log_event(
                    logger,
                    "archive_ok",
                    request_id=request_id,
                    package_name=package,
                    version_name=version_name,
                    version_code=version_code,
                    artifact_path=str(artifact),
                )
                return
            except Exception as exc:  # noqa: BLE001 — 归档失败绝不影响刷新/用户请求
                if attempt >= self.max_retries:
                    log_event(
                        logger,
                        "archive_failed",
                        request_id=request_id,
                        package_name=package,
                        version_name=version_name,
                        version_code=version_code,
                        message=str(exc),
                    )
                    return

    def _build_orchestrator(self) -> DownloadOrchestrator:
        if self._orchestrator is None:
            self._orchestrator = DownloadOrchestrator(
                CatalogStore(self.settings.catalog_db_path),
                ProviderFactory(self.settings),
                PackageDownloader(self.settings),
            )
        return self._orchestrator
