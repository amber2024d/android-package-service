from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse

from app.api.errors import error_response
from app.core.config import Settings, get_settings
from app.domain.errors import AggregateProviderError, ProviderException
from app.domain.models import AndroidPackageRequest
from app.download.downloader import PackageDownloader
from app.providers.factory import ProviderFactory

router = APIRouter(prefix="/api/v1/android")


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
    package_name: str,
    version_code: int | None = Query(default=None, alias="versionCode"),
    version_name: str | None = Query(default=None, alias="versionName"),
    provider: str | None = Query(default=None),
    factory: ProviderFactory = Depends(get_provider_factory),
):
    request = request_from_query(package_name, version_code, version_name, provider)
    try:
        result = await factory.get_package_info(request)
        return JSONResponse(result.model_dump(mode="json", by_alias=True))
    except AggregateProviderError as exc:
        return error_response(exc)


@router.get("/apps/{package_name}/files")
async def get_files(
    package_name: str,
    version_code: int | None = Query(default=None, alias="versionCode"),
    version_name: str | None = Query(default=None, alias="versionName"),
    provider: str | None = Query(default=None),
    factory: ProviderFactory = Depends(get_provider_factory),
):
    request = request_from_query(package_name, version_code, version_name, provider)
    try:
        result = await factory.get_download_plan(request)
        return JSONResponse(result.model_dump(mode="json", by_alias=True))
    except AggregateProviderError as exc:
        return error_response(exc)


@router.get("/apps/{package_name}/download")
async def download_app(
    package_name: str,
    version_code: int | None = Query(default=None, alias="versionCode"),
    version_name: str | None = Query(default=None, alias="versionName"),
    provider: str | None = Query(default=None),
    factory: ProviderFactory = Depends(get_provider_factory),
    downloader: PackageDownloader = Depends(get_downloader),
):
    request = request_from_query(package_name, version_code, version_name, provider)
    errors = []
    try:
        providers = factory.resolve(request.preferred_provider)
    except AggregateProviderError as exc:
        return error_response(exc)

    for resolved_provider in providers:
        try:
            plan = await resolved_provider.get_download_plan(request)
            artifact = await downloader.download(plan)
            return FileResponse(
                artifact,
                media_type=media_type_for(artifact),
                filename=artifact.name,
            )
        except ProviderException as exc:
            errors.append(exc.provider_error)
    return error_response(AggregateProviderError(errors))


def media_type_for(path: Path) -> str:
    if path.suffix == ".apk":
        return "application/vnd.android.package-archive"
    return "application/zip"
