import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api.errors import error_response
from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.domain.errors import AggregateProviderError, ProviderException
from app.domain.models import AndroidPackageRequest
from app.download.downloader import PackageDownloader
from app.providers.factory import ProviderFactory

router = APIRouter(prefix="/api/v1/android")
logger = logging.getLogger(__name__)


def get_provider_factory(settings: Settings = Depends(get_settings)) -> ProviderFactory:
    return ProviderFactory(settings)


def get_downloader(settings: Settings = Depends(get_settings)) -> PackageDownloader:
    return PackageDownloader(settings)


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
    factory: ProviderFactory = Depends(get_provider_factory),
    downloader: PackageDownloader = Depends(get_downloader),
):
    request_id = request_id_for(http_request)
    request = request_from_query(package_name, version_code, version_name, provider)
    errors = []
    try:
        providers = factory.resolve(request.preferred_provider)
    except AggregateProviderError as exc:
        log_provider_failure(request_id, request, exc)
        return error_response(exc)

    for resolved_provider in providers:
        try:
            plan = await resolved_provider.get_download_plan(request)
            artifact = await downloader.download(plan, request_id=request_id)
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
            return FileResponse(
                artifact,
                media_type=media_type_for(artifact),
                filename=artifact.name,
            )
        except ProviderException as exc:
            errors.append(exc.provider_error)
            log_event(
                logger,
                "provider_failed",
                request_id=request_id,
                package_name=package_name,
                version_code=version_code,
                version_name=version_name,
                provider=exc.provider_error.provider,
                upstream_status=exc.provider_error.error.value,
                message=exc.provider_error.message,
            )
    return error_response(AggregateProviderError(errors))


def media_type_for(path: Path) -> str:
    if path.suffix == ".apk":
        return "application/vnd.android.package-archive"
    return "application/zip"


def request_id_for(request: Request) -> str:
    request_id = request.headers.get("x-request-id") or str(uuid4())
    request.state.request_id = request_id
    return request_id


def log_provider_failure(
    request_id: str,
    request: AndroidPackageRequest,
    error: AggregateProviderError,
) -> None:
    for provider_error in error.provider_errors:
        log_event(
            logger,
            "provider_failed",
            request_id=request_id,
            package_name=request.package_name,
            version_code=request.version_code,
            version_name=request.version_name,
            provider=provider_error.provider,
            upstream_status=provider_error.error.value,
            message=provider_error.message,
        )
