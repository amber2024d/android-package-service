from typing import NoReturn
from urllib.parse import quote, urlencode

from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import (
    AndroidPackageInfo,
    AndroidPackageRequest,
    DownloadPlan,
    PackageFile,
    PackageFileType,
    PackageVersion,
)
from app.providers import apkmirror_versions
from app.providers.apkpure_versions import WEB_DOWNLOAD_HEADERS
from app.providers.base import AndroidPackageProvider


class APKMirrorProvider(AndroidPackageProvider):
    """APKMirror 纯下载 provider：更深的 downloadable 历史源（低优先级 fallback）。

    下载键就是 versionName：有名优先「拼 release URL 直命中」（省 uploads 翻页），失败再列表按名兜底；
    走 release → 变体 → 4 跳直链，产物是 `.apkm` bundle（下载层解包重建 `.xapk`）。
    按 versionCode 单独请求无法定位（列表无 code）→ NOT_FOUND，让位其它源。
    """

    id = "apkmirror"

    def __init__(
        self,
        priority: int = 15,
        enabled: bool = True,  # 「默认关」由 settings.provider_apkmirror_enabled 表达；工厂只在开关开时实例化
        timeout_seconds: float = 120.0,
        user_agent: str = "AndroidPackageService/0.1.0",
        proxy: str | None = None,
    ):
        self.priority = priority
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.proxy = proxy

    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        _dev_slug, app_slug = await self._slugs(request.package_name)
        versions = await self._list(request.package_name, app_slug)
        selected = self._select(versions, request)
        return AndroidPackageInfo(
            package_name=request.package_name,
            app_name=request.package_name,
            version_name=selected.version_name,
            version_code=selected.version_code,
            provider=self.id,
            download_url=self._download_url(request.package_name, version_name=selected.version_name),
            versions=[
                PackageVersion(
                    version_name=version.version_name,
                    version_code=version.version_code,
                    download_url=self._download_url(request.package_name, version_name=version.version_name),
                )
                for version in versions
            ],
        )

    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        dev_slug, app_slug = await self._slugs(request.package_name)
        release_url = await self._release_url(request, dev_slug, app_slug)
        final_url, download = await self._resolve_download(release_url, request, app_slug)

        version_name = request.version_name or download.version_name
        version_code = request.version_code if request.version_code is not None else download.version_code
        return DownloadPlan(
            package_name=request.package_name,
            app_name=request.package_name,
            version_name=version_name,
            version_code=version_code,
            provider=self.id,
            files=[
                PackageFile(
                    type=PackageFileType.APKM,
                    name="bundle.apkm",
                    url=final_url,
                    headers={**WEB_DOWNLOAD_HEADERS, "Referer": download.intermediate_url},
                    proxy=self.proxy,
                    metadata={
                        "bundle.format": "apkm",
                        "asset.type": "APKM",
                        "download.fallback": "wget",
                        "source.release_url": release_url,
                    },
                )
            ],
        )

    async def _release_url(self, request: AndroidPackageRequest, dev_slug: str, app_slug: str) -> str:
        if request.version_name:
            # 拼 URL 优先（省 uploads 翻页），命中失败时由 _resolve_download 列表兜底。
            return apkmirror_versions.construct_release_url(dev_slug, app_slug, request.version_name)
        if request.version_code is not None:
            self._fail(ErrorCode.NOT_FOUND, "APKMirror cannot resolve by versionCode alone.")
        # 最新版：uploads 第一行（最新在前）。
        versions = await self._list(request.package_name, app_slug)
        if not versions:
            self._fail(ErrorCode.NOT_FOUND, "APKMirror has no versions for package.")
        return versions[0].release_url

    async def _resolve_download(self, release_url: str, request: AndroidPackageRequest, app_slug: str):
        try:
            return await apkmirror_versions.resolve_download_url(
                release_url,
                provider_id=self.id,
                user_agent=self.user_agent,
                timeout_seconds=self.timeout_seconds,
                proxy=self.proxy,
            )
        except ProviderException:
            # 拼的 release URL 不对时，列表按名兜底找真实 release_url。
            if not request.version_name:
                raise
            versions = await self._list(request.package_name, app_slug)
            match = next((v for v in versions if v.version_name == request.version_name), None)
            if match is None or match.release_url == release_url:
                self._fail(ErrorCode.NOT_FOUND, "APKMirror has no matching version.")
            return await apkmirror_versions.resolve_download_url(
                match.release_url,
                provider_id=self.id,
                user_agent=self.user_agent,
                timeout_seconds=self.timeout_seconds,
                proxy=self.proxy,
            )

    def _select(
        self, versions: list[apkmirror_versions.APKMirrorVersion], request: AndroidPackageRequest
    ) -> apkmirror_versions.APKMirrorVersion:
        if not versions:
            self._fail(ErrorCode.NOT_FOUND, "APKMirror has no versions for package.")
        if request.version_name is None and request.version_code is None:
            return versions[0]
        match = next((v for v in versions if v.version_name == request.version_name), None)
        if match is None:
            self._fail(ErrorCode.NOT_FOUND, "APKMirror has no matching version.")
        return match

    async def _slugs(self, package_name: str) -> tuple[str, str]:
        return await apkmirror_versions.resolve_slugs(
            package_name,
            provider_id=self.id,
            user_agent=self.user_agent,
            timeout_seconds=self.timeout_seconds,
            proxy=self.proxy,
        )

    async def _list(self, package_name: str, app_slug: str) -> list[apkmirror_versions.APKMirrorVersion]:
        return await apkmirror_versions.list_versions(
            package_name,
            app_slug,
            provider_id=self.id,
            user_agent=self.user_agent,
            timeout_seconds=self.timeout_seconds,
            proxy=self.proxy,
        )

    def _download_url(self, package_name: str, version_name: str | None = None) -> str:
        params: dict[str, str] = {"provider": self.id}
        if version_name:
            params["versionName"] = version_name
        return f"/api/v1/android/apps/{quote(package_name, safe='')}/download?{urlencode(params)}"

    def _fail(self, error: ErrorCode, message: str) -> NoReturn:
        raise ProviderException(ProviderError(provider=self.id, error=error, message=message))
