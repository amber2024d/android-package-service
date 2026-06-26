import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api.errors import error_response
from app.catalog.catalog import VersionCatalog
from app.catalog.orchestrator import DownloadOrchestrator
from app.catalog.runtime import build_catalog
from app.catalog.store import CatalogStore
from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.domain.errors import AggregateProviderError
from app.domain.models import AndroidPackageRequest, CatalogVersion, CatalogVersionsResponse
from app.download.downloader import PackageDownloader
from app.providers.factory import ProviderFactory

router = APIRouter(prefix="/api/v1/android")
logger = logging.getLogger(__name__)

# fire-and-forget 的后台收集任务：保持强引用防止被 GC，完成后回收并记录异常（§H ③ 触发即忘）。
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_done)


def _background_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log_event(logger, "catalog_collect_failed", message=str(exc))


def get_provider_factory(settings: Settings = Depends(get_settings)) -> ProviderFactory:
    return ProviderFactory(settings)


def get_downloader(settings: Settings = Depends(get_settings)) -> PackageDownloader:
    return PackageDownloader(settings)


def get_orchestrator(
    settings: Settings = Depends(get_settings),
    factory: ProviderFactory = Depends(get_provider_factory),
    downloader: PackageDownloader = Depends(get_downloader),
) -> DownloadOrchestrator:
    return DownloadOrchestrator(CatalogStore(settings.catalog_db_path), factory, downloader)


def get_catalog(settings: Settings = Depends(get_settings)) -> VersionCatalog:
    return build_catalog(settings)


def request_from_query(
    package_name: str,
    version_code: int | None,
    version_name: str | None,
    provider: str | None,
) -> AndroidPackageRequest:
    return AndroidPackageRequest(
        package_name=package_name,
        version_code=version_code,
        version_name=version_name,
        preferred_provider=provider,
    )


@router.get("/apps/{package_name}")
async def get_app(
    http_request: Request,
    package_name: str,
    version_code: int | None = Query(default=None, alias="versionCode"),
    version_name: str | None = Query(default=None, alias="versionName"),
    provider: str | None = Query(default=None),
    factory: ProviderFactory = Depends(get_provider_factory),
):
    request_id = request_id_for(http_request)
    request = request_from_query(package_name, version_code, version_name, provider)
    try:
        result = await factory.get_package_info(request, request_id=request_id)
        log_event(
            logger,
            "package_info_ok",
            request_id=request_id,
            package_name=package_name,
            version_code=result.version_code,
            version_name=result.version_name,
            provider=result.provider,
            upstream_status="ok",
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True))
    except AggregateProviderError as exc:
        return error_response(exc)


@router.get("/apps/{package_name}/files")
async def get_files(
    http_request: Request,
    package_name: str,
    version_code: int | None = Query(default=None, alias="versionCode"),
    version_name: str | None = Query(default=None, alias="versionName"),
    provider: str | None = Query(default=None),
    factory: ProviderFactory = Depends(get_provider_factory),
):
    request_id = request_id_for(http_request)
    request = request_from_query(package_name, version_code, version_name, provider)
    try:
        result = await factory.get_download_plan(request, request_id=request_id)
        log_event(
            logger,
            "download_plan_ok",
            request_id=request_id,
            package_name=package_name,
            version_code=result.version_code,
            version_name=result.version_name,
            provider=result.provider,
            upstream_status="ok",
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True))
    except AggregateProviderError as exc:
        return error_response(exc)


@router.get("/apps/{package_name}/versions")
async def list_app_versions(
    http_request: Request,
    package_name: str,
    catalog: VersionCatalog = Depends(get_catalog),
):
    request_id = request_id_for(http_request)
    # 从没采过的包首采阻塞一次（need_history=False：不在读路径触发增量刷新，新鲜度交阶段 14 定时任务）；
    # 已跟踪包直接读库返回。
    await catalog.ensure_collected(package_name, need_history=False)
    versions = [
        CatalogVersion(version_name=name, version_code=code, release_date=release_date)
        for name, code, release_date in catalog.list_downloadable(package_name)
    ]
    log_event(
        logger,
        "catalog_versions_ok",
        request_id=request_id,
        package_name=package_name,
        upstream_status="ok",
        version_count=len(versions),
    )
    response = CatalogVersionsResponse(package_name=package_name, versions=versions)
    return JSONResponse(response.model_dump(mode="json", by_alias=True))


@router.get("/apps/{package_name}/download")
async def download_app(
    http_request: Request,
    package_name: str,
    version_code: int | None = Query(default=None, alias="versionCode"),
    version_name: str | None = Query(default=None, alias="versionName"),
    provider: str | None = Query(default=None),
    orchestrator: DownloadOrchestrator = Depends(get_orchestrator),
    catalog: VersionCatalog = Depends(get_catalog),
):
    request_id = request_id_for(http_request)
    request = request_from_query(package_name, version_code, version_name, provider)
    # 指定版本（决策②）：后台异步补目录，与下载并发、不阻塞响应；收集单飞去重（与定时任务共用同一把）。
    # 不传版本=最新版快路径，不触发收集。
    if version_code is not None or version_name is not None:
        _spawn_background(catalog.ensure_collected(package_name, need_history=True))
    try:
        artifact = await orchestrator.download(request, request_id=request_id)
    except AggregateProviderError as exc:
        # 编排器已逐条记 provider_failed / 解析失败；这里只负责出错误响应。
        return error_response(exc)
    return FileResponse(
        artifact,
        media_type=media_type_for(artifact),
        filename=artifact.name,
    )


def media_type_for(path: Path) -> str:
    if path.suffix == ".apk":
        return "application/vnd.android.package-archive"
    return "application/zip"


def request_id_for(request: Request) -> str:
    request_id = request.headers.get("x-request-id") or str(uuid4())
    request.state.request_id = request_id
    return request_id
