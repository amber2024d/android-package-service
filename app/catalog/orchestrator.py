import logging
from contextlib import closing
from pathlib import Path
from sqlite3 import Connection

from app.catalog.store import CatalogStore
from app.core.logging import log_event
from app.domain.errors import AggregateProviderError, ProviderError, ProviderException
from app.domain.models import AndroidPackageRequest
from app.download.downloader import PackageDownloader
from app.providers.factory import ProviderFactory

logger = logging.getLogger(__name__)


class DownloadOrchestrator:
    """下载编排器（阶段 12）：集中做 name↔code 补全 + 选源 + 下载锁归一 + 优先级 fallback。

    - **补全**：用目录的 `ledger`（权威）与 `versions`（采集）把请求缺失的 versionCode / versionName 补齐
      （有就用、查不到不阻塞）。补全后「按名」「按号」指向同一版本的请求会归一到同一把下载锁与同一份 artifact。
    - **选源/兜底**：复用 provider 工厂的优先级 fallback；provider 仍各自按（补全后的）版本自解析本源下载键并取文件，
      编排器不替它们枚举。
    - **归一**：把补全后的版本引用（code 优先）作为下载锁 key 传给下载层（§H ①）。

    冷目录（账本/库为空）时补全为空操作，行为与重构前等价。
    """

    def __init__(self, store: CatalogStore, factory: ProviderFactory, downloader: PackageDownloader):
        self.store = store
        self.factory = factory
        self.downloader = downloader

    async def download(self, request: AndroidPackageRequest, request_id: str | None = None) -> Path:
        completed = self._complete(request)
        lock_version_key = (
            str(completed.version_code) if completed.version_code is not None else completed.version_name
        )
        try:
            providers = self.factory.resolve(completed.preferred_provider)
        except AggregateProviderError as exc:
            self._log_failures(completed, exc.provider_errors, request_id)
            raise

        errors: list[ProviderError] = []
        for provider in providers:
            try:
                plan = await provider.get_download_plan(completed)
                artifact = await self.downloader.download(plan, request_id=request_id, lock_version_key=lock_version_key)
                log_event(
                    logger,
                    "download_ok",
                    request_id=request_id,
                    package_name=plan.package_name,
                    version_code=plan.version_code,
                    version_name=plan.version_name,
                    provider=plan.provider,
                    upstream_status="ok",
                    artifact_path=str(artifact),
                )
                return artifact
            except ProviderException as exc:
                errors.append(exc.provider_error)
                self._log_failures(completed, [exc.provider_error], request_id)
        raise AggregateProviderError(errors)

    # ---- name↔code 补全 ------------------------------------------------------- #

    def _complete(self, request: AndroidPackageRequest) -> AndroidPackageRequest:
        code, name = request.version_code, request.version_name
        # 都空（最新）或都有：无需补全，省一次库读。
        if (code is None) == (name is None):
            return request
        with closing(self.store.connect()) as conn:
            if code is None:
                code = self._lookup_code(conn, request.package_name, name)
            else:
                name = self._lookup_name(conn, request.package_name, code)
        if code == request.version_code and name == request.version_name:
            return request
        return AndroidPackageRequest(
            package_name=request.package_name,
            version_code=code,
            version_name=name,
            preferred_provider=request.preferred_provider,
        )

    def _lookup_code(self, conn: Connection, package: str, version_name: str) -> int | None:
        # 账本权威优先；一名多号（§3.2）取最高 code（最近构建，最可能可下）。
        row = conn.execute(
            "SELECT version_code FROM ledger WHERE package = ? AND version_name = ? AND version_code IS NOT NULL "
            "ORDER BY version_code DESC LIMIT 1",
            (package, version_name),
        ).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "SELECT version_code FROM versions WHERE package = ? AND version_name = ? AND version_code IS NOT NULL "
            "ORDER BY version_code DESC LIMIT 1",
            (package, version_name),
        ).fetchone()
        return row[0] if row else None

    def _lookup_name(self, conn: Connection, package: str, version_code: int) -> str | None:
        row = conn.execute(
            "SELECT version_name FROM ledger WHERE package = ? AND version_code = ? LIMIT 1",
            (package, version_code),
        ).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "SELECT version_name FROM versions WHERE package = ? AND version_code = ? LIMIT 1",
            (package, version_code),
        ).fetchone()
        return row[0] if row else None

    def _log_failures(self, request: AndroidPackageRequest, errors: list[ProviderError], request_id: str | None) -> None:
        for error in errors:
            log_event(
                logger,
                "provider_failed",
                request_id=request_id,
                package_name=request.package_name,
                version_code=request.version_code,
                version_name=request.version_name,
                provider=error.provider,
                upstream_status=error.error.value,
                message=error.message,
            )
