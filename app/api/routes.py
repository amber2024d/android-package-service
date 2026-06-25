import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api.errors import error_response
from app.catalog.orchestrator import DownloadOrchestrator
from app.catalog.store import CatalogStore
from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.domain.errors import AggregateProviderError
from app.domain.models import AndroidPackageRequest
from app.download.downloader import PackageDownloader
from app.providers.factory import ProviderFactory

router = APIRouter(prefix="/api/v1/android")
logger = logging.getLogger(__name__)


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


@router.get("/apps/{package_name}/download")
async def download_app(
    http_request: Request,
    package_name: str,
    version_code: int | None = Query(default=None, alias="versionCode"),
    version_name: str | None = Query(default=None, alias="versionName"),
    provider: str | None = Query(default=None),
    orchestrator: DownloadOrchestrator = Depends(get_orchestrator),
):
    request_id = request_id_for(http_request)
    request = request_from_query(package_name, version_code, version_name, provider)
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
