import logging
from contextlib import closing
from pathlib import Path
from sqlite3 import Connection

from app.catalog.store import CatalogStore
from app.core.logging import log_event
from app.domain.errors import AggregateProviderError, ErrorCode, ProviderError, ProviderException
from app.domain.models import AndroidPackageRequest, DownloadPlan
from app.download.downloader import PackageDownloader
from app.providers.factory import ProviderFactory

logger = logging.getLogger(__name__)
NETWORK_ERROR_RETRIES = 3


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

    async def plan(self, request: AndroidPackageRequest, request_id: str | None = None) -> DownloadPlan:
        """`/files` 用：与下载走同一套名↔号补全，让两端的 version_code 报告与产物缓存 key 一致。"""
        completed = self._complete(request)
        return await self.factory.get_download_plan(completed, request_id=request_id)

    async def download(self, request: AndroidPackageRequest, request_id: str | None = None) -> tuple[Path, str]:
        """返回 (artifact, 命中 provider)。命中 provider = 复用产物所属源或 fallback 后实际下载成功的源，
        供下载 worker 直接落库（监控的「命中来源」据此还原，不再靠 artifact 路径解析）。"""
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
            cached = self._existing_artifact(provider.id, completed)
            if cached is not None:
                # 命中已下产物：跳过 provider 抓取与重下，直接复用（决策①②）。
                self._log_reuse(completed, provider.id, cached, request_id)
                return cached, provider.id
            for retry in range(NETWORK_ERROR_RETRIES + 1):
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
                    return artifact, plan.provider
                except ProviderException as exc:
                    if exc.provider_error.error == ErrorCode.NETWORK_ERROR and retry < NETWORK_ERROR_RETRIES:
                        log_event(
                            logger,
                            "provider_retry",
                            request_id=request_id,
                            package_name=completed.package_name,
                            version_code=completed.version_code,
                            version_name=completed.version_name,
                            provider=provider.id,
                            upstream_status=ErrorCode.NETWORK_ERROR.value,
                            retry=retry + 1,
                            max_retries=NETWORK_ERROR_RETRIES,
                            message=exc.provider_error.message,
                        )
                        continue
                    errors.append(exc.provider_error)
                    self._log_failures(completed, [exc.provider_error], request_id)
                    break
        raise AggregateProviderError(errors)

    def existing(self, request: AndroidPackageRequest) -> Path | None:
        """只探已落 artifact，不解析 provider、不下载。给 Web 入队前保留缓存快路径。"""
        completed = self._complete(request)
        providers = self.factory.resolve(completed.preferred_provider)
        for provider in providers:
            artifact = self._existing_artifact(provider.id, completed)
            if artifact is not None:
                return artifact
        return None

    # ---- 抓取前先探已有产物（决策①②） --------------------------------------- #

    def _existing_artifact(self, provider_id: str, completed: AndroidPackageRequest) -> Path | None:
        """指定版本时，抓取前先探这个 provider 已落 NAS 的产物，命中即复用、跳过整段 provider 解析。

        同一逻辑版本的 ``version_key`` 可能被写成 ``versionCode`` 或 ``versionName`` 两种
        （冷目录首下按名、暖后按号；ledger 回填又可能补出号），所以按 code、name 各探一次，
        消除「同版本两种 key 而重下」。latest（无版本）不在此短路，避免复用过期的最新版产物。
        """
        candidates: list[tuple[int | None, str | None]] = []
        if completed.version_code is not None:
            candidates.append((completed.version_code, completed.version_name))
        if completed.version_name is not None:
            candidates.append((None, completed.version_name))

        seen: set[str] = set()
        for code, name in candidates:
            probe = DownloadPlan(
                package_name=completed.package_name,
                app_name=completed.package_name,
                version_code=code,
                version_name=name,
                provider=provider_id,
                files=[],
            )
            if probe.version_key in seen:
                continue
            seen.add(probe.version_key)
            artifact = self.downloader.existing(probe)
            if artifact is not None:
                return artifact
        return None

    def _log_reuse(
        self, request: AndroidPackageRequest, provider_id: str, artifact: Path, request_id: str | None
    ) -> None:
        # 与下载层 existing() 命中同形态：artifact_reused（产物级）+ download_ok（请求级）。
        for event, status in (("artifact_reused", "reused"), ("download_ok", "reused")):
            log_event(
                logger,
                event,
                request_id=request_id,
                package_name=request.package_name,
                version_code=request.version_code,
                version_name=request.version_name,
                provider=provider_id,
                upstream_status=status,
                artifact_path=str(artifact),
            )

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
