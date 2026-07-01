import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse

from app.api.errors import error_response
from app.auth.deps import require_api_key
from app.storage.factory import build_storage_backend
from app.catalog.catalog import VersionCatalog
from app.catalog.orchestrator import DownloadOrchestrator
from app.catalog.runtime import build_catalog
from app.catalog.store import CatalogStore
from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.domain.errors import AggregateProviderError
from app.domain.models import AndroidPackageRequest, CatalogVersion, CatalogVersionsResponse
from app.download.downloader import PackageDownloader
from app.download.jobs import DownloadJob, DownloadJobStore
from app.providers.factory import ProviderFactory

# 数据 API 全部需 API Key（§3.2/§3.4）；router 级依赖统一覆盖 6 个端点。
# auth_enabled / auth_api_key_enabled 关时 require_api_key 放行（内网/测试兼容）。
router = APIRouter(prefix="/api/v1/android", dependencies=[Depends(require_api_key)])
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


def get_download_jobs(settings: Settings = Depends(get_settings)) -> DownloadJobStore:
    return DownloadJobStore(CatalogStore(settings.catalog_db_path), lease_seconds=settings.download_job_lease_seconds)


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
    orchestrator: DownloadOrchestrator = Depends(get_orchestrator),
):
    request_id = request_id_for(http_request)
    request = request_from_query(package_name, version_code, version_name, provider)
    try:
        result = await orchestrator.plan(request, request_id=request_id)
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
    settings: Settings = Depends(get_settings),
    orchestrator: DownloadOrchestrator = Depends(get_orchestrator),
    jobs: DownloadJobStore = Depends(get_download_jobs),
    catalog: VersionCatalog = Depends(get_catalog),
):
    request_id = request_id_for(http_request)
    request = request_from_query(package_name, version_code, version_name, provider)
    # 指定版本（决策②）：后台异步补目录，与下载并发、不阻塞响应；收集单飞去重（与定时任务共用同一把）。
    # 不传版本=最新版快路径，不触发收集。
    if version_code is not None or version_name is not None:
        _spawn_background(catalog.ensure_collected(package_name, need_history=True))
    if not settings.download_async_enabled:
        try:
            artifact, _ = await orchestrator.download(request, request_id=request_id)
        except AggregateProviderError as exc:
            return error_response(exc)
        return artifact_response(settings, artifact)
    try:
        artifact = orchestrator.existing(request)
    except AggregateProviderError as exc:
        return error_response(exc)
    if artifact is None:
        job = jobs.enqueue(request, request_id)
        log_event(
            logger,
            "download_queued",
            request_id=request_id,
            job_id=job.id,
            package_name=package_name,
            version_code=version_code,
            version_name=version_name,
            provider=provider,
        )
        return JSONResponse(download_job_response(http_request, job), status_code=202)
    log_event(
        logger,
        "download_ready",
        request_id=request_id,
        package_name=package_name,
        version_code=version_code,
        version_name=version_name,
        artifact_path=str(artifact),
    )
    return artifact_response(settings, artifact)


@router.get("/downloads/{job_id}")
async def get_download_job(
    http_request: Request,
    job_id: str,
    jobs: DownloadJobStore = Depends(get_download_jobs),
):
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "NOT_FOUND", "message": "Download job not found."}, status_code=404)
    return JSONResponse(download_job_response(http_request, job))


@router.get("/downloads/{job_id}/file")
async def get_download_job_file(
    job_id: str,
    settings: Settings = Depends(get_settings),
    jobs: DownloadJobStore = Depends(get_download_jobs),
):
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "NOT_FOUND", "message": "Download job not found."}, status_code=404)
    key = str(job.artifact_path) if job.artifact_path is not None else None
    if job.status != "succeeded" or key is None or not build_storage_backend(settings).exists(key):
        return JSONResponse({"error": "NOT_READY", "message": "Download job has no ready artifact."}, status_code=409)
    return artifact_response(settings, key)


def artifact_response(settings: Settings, key: str):
    """按存储后端能力下发产物 key（§4.5）：能签名 → 302 signed URL；本地 → NAS 直链 302 或 FileResponse。"""
    backend = build_storage_backend(settings)
    filename = key.rsplit("/", 1)[-1]
    signed = backend.signed_url(key, expires_in=settings.signed_url_ttl_seconds, filename=filename)
    if signed:
        return RedirectResponse(signed, status_code=302)
    local = backend.local_path(key)
    if local is not None:
        # 配了 NAS 直供前缀就 302 到 NAS nginx 直链，否则本服务 FileResponse 流式返回。
        nas_url = nas_public_url(settings, local)
        if nas_url:
            return RedirectResponse(nas_url, status_code=302)
        return FileResponse(local, media_type=media_type_for(local), filename=local.name)
    # 对象后端但未签名（异常兜底）：流式转发。
    return StreamingResponse(backend.open_stream(key), media_type=media_type_for(Path(filename)))


def download_job_response(request: Request, job: DownloadJob) -> dict:
    base = str(request.base_url).rstrip("/")
    status_url = f"{base}/api/v1/android/downloads/{job.id}"
    file_url = f"{status_url}/file" if job.status == "succeeded" else None
    return {
        "jobId": job.id,
        "status": job.status,
        "packageName": job.request.package_name,
        "versionCode": job.request.version_code,
        "versionName": job.request.version_name,
        "provider": job.request.preferred_provider,
        "statusUrl": status_url,
        "fileUrl": file_url,
        "artifactPath": str(job.artifact_path) if job.artifact_path else None,
        "error": job.error,
        "providerErrors": [
            error.model_dump(mode="json") for error in job.provider_errors
        ] if job.provider_errors else [],
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
        "startedAt": job.started_at,
        "finishedAt": job.finished_at,
    }


def nas_public_url(settings: Settings, artifact: Path) -> str | None:
    """把 NAS 上的 artifact 路径映射成 NAS HTTP 服务直链；未配前缀或 artifact 不在 NAS 挂载下时返回 None。"""
    base = settings.nas_public_base_url
    if not base:
        return None
    try:
        relative = artifact.relative_to(settings.nas_mount_path)
    except ValueError:
        return None
    return base.rstrip("/") + "/" + "/".join(quote(part) for part in relative.parts)


def media_type_for(path: Path) -> str:
    if path.suffix == ".apk":
        return "application/vnd.android.package-archive"
    return "application/zip"


def request_id_for(request: Request) -> str:
    request_id = request.headers.get("x-request-id") or str(uuid4())
    request.state.request_id = request_id
    return request_id
