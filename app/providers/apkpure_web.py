from dataclasses import dataclass
from html import unescape
import re
from typing import NoReturn
from urllib.parse import quote, urlencode, urljoin

from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import (
    AndroidPackageInfo,
    AndroidPackageRequest,
    DownloadPlan,
    PackageFile,
    PackageFileType,
    PackageVersion,
)
from app.providers import apkpure_versions
from app.providers.apkpure_versions import WEB_DOWNLOAD_HEADERS
from app.providers.base import AndroidPackageProvider


@dataclass(frozen=True)
class APKPureWebDetail:
    package_name: str
    app_name: str
    detail_url: str
    version_name: str | None
    version_code: int | None
    raw_file_type: str | None
    download_page_url: str


class APKPureWebProvider(AndroidPackageProvider):
    id = "apkpure-web"
    base_url = "https://apkpure.com"

    def __init__(
        self,
        priority: int = 20,
        enabled: bool = True,
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
        detail = await self._load_detail(request.package_name)
        if self._is_historical(detail, request):
            return await self._historical_package_info(detail, request)
        return AndroidPackageInfo(
            package_name=detail.package_name,
            app_name=detail.app_name,
            version_name=detail.version_name,
            version_code=detail.version_code,
            provider=self.id,
            download_url=self._api_download_url(detail.package_name),
            versions=[
                PackageVersion(
                    version_code=detail.version_code,
                    version_name=detail.version_name,
                    download_url=self._api_download_url(
                        detail.package_name,
                        version_code=detail.version_code,
                        version_name=detail.version_name,
                    ),
                )
            ],
        )

    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        detail = await self._load_detail(request.package_name)
        if self._is_historical(detail, request):
            return await self._historical_download_plan(detail, request)

        download_html = await self._load_html(detail.download_page_url)
        url = self._download_url_from_html(download_html)
        content_disposition = await self._content_disposition(url) if url else None

        if not url and detail.version_code is not None:
            file_type, url, content_disposition = await self._constructed_download(
                detail.package_name,
                detail.version_code,
                detail.raw_file_type,
            )

        file_type = self._file_type(detail.raw_file_type, url, content_disposition)
        if not url:
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure web download url is missing.")

        return DownloadPlan(
            package_name=detail.package_name,
            app_name=detail.app_name,
            version_name=detail.version_name,
            version_code=detail.version_code,
            provider=self.id,
            files=[
                PackageFile(
                    type=file_type,
                    name=self._file_name(file_type),
                    url=url,
                    headers={**WEB_DOWNLOAD_HEADERS, "Referer": detail.download_page_url},
                    proxy=self.proxy,
                    metadata={"asset.type": file_type.value, "download.fallback": "wget"},
                )
            ],
        )

    # -- 历史版本：走共享版本目录（/versions 页抓取 + 各版本下载页解析） --

    def _is_historical(self, detail: APKPureWebDetail, request: AndroidPackageRequest) -> bool:
        # 本源权威键单键判定：号是否最新优先看 versionCode（双方都有就只比号），缺号才退比 versionName。
        # 不再「号不等 OR 名不等」——否则编排器补全出的 name 跨源漂移（1.17 vs 1.17.0）会把
        # 已命中最新 code 的请求误推进历史网页抓取分支，白跑一趟 Cloudflare 还让版本元数据偏移。
        if request.version_code is not None and detail.version_code is not None:
            return request.version_code != detail.version_code
        if request.version_name is not None:
            return request.version_name != detail.version_name
        return request.version_code is not None and request.version_code != detail.version_code

    async def _historical_versions(self, detail: APKPureWebDetail) -> list[apkpure_versions.APKPureVersion]:
        return await apkpure_versions.list_versions(
            detail.detail_url,
            detail.package_name,
            provider_id=self.id,
            user_agent=self.user_agent,
            timeout_seconds=self.timeout_seconds,
            proxy=self.proxy,
        )

    async def _historical_download_plan(self, detail: APKPureWebDetail, request: AndroidPackageRequest) -> DownloadPlan:
        version = await self._resolve_historical(detail, request)
        package_file = await apkpure_versions.resolve_version_file(
            version,
            provider_id=self.id,
            user_agent=self.user_agent,
            timeout_seconds=self.timeout_seconds,
            proxy=self.proxy,
        )
        return DownloadPlan(
            package_name=detail.package_name,
            app_name=detail.app_name,
            version_name=version.version_name,
            version_code=version.version_code,
            provider=self.id,
            files=[package_file],
        )

    async def _resolve_historical(
        self, detail: APKPureWebDetail, request: AndroidPackageRequest
    ) -> apkpure_versions.APKPureVersion:
        """纯下载化（阶段 12 收口）：有 versionName 就复用已加载详情页的 detail_url 直命中
        `/download/{name}`，不再抓 `/versions` 全量枚举（§10）。只有「按 code 且目录冷、补不出名」
        才窄兜底回枚举按 code 找——保不回归。
        """
        if request.version_name:
            return apkpure_versions.APKPureVersion(
                package_name=detail.package_name,
                version_name=request.version_name,
                version_code=request.version_code,
                apkid="",
                file_type=PackageFileType.BASE_APK,
                detail_url=detail.detail_url,
            )
        return self._select_historical(await self._historical_versions(detail), request)

    async def _historical_package_info(self, detail: APKPureWebDetail, request: AndroidPackageRequest) -> AndroidPackageInfo:
        versions = await self._historical_versions(detail)
        selected = self._select_historical(versions, request)
        return AndroidPackageInfo(
            package_name=detail.package_name,
            app_name=detail.app_name,
            version_name=selected.version_name,
            version_code=selected.version_code,
            provider=self.id,
            download_url=self._api_download_url(
                detail.package_name,
                version_code=selected.version_code,
                version_name=selected.version_name,
            ),
            versions=[
                PackageVersion(
                    version_code=version.version_code,
                    version_name=version.version_name,
                    download_url=self._api_download_url(
                        detail.package_name,
                        version_code=version.version_code,
                        version_name=version.version_name,
                    ),
                )
                for version in versions
            ],
        )

    def _select_historical(
        self, versions: list[apkpure_versions.APKPureVersion], request: AndroidPackageRequest
    ) -> apkpure_versions.APKPureVersion:
        version = apkpure_versions.select_version(
            versions, version_code=request.version_code, version_name=request.version_name
        )
        if version is None:
            self._fail(ErrorCode.NOT_FOUND, "APKPure web has no matching historical version.")
        return version

    # -- 下载页/详情页加载与解析（委托共享模块，保留实例方法便于测试打桩） --

    async def _load_detail(self, package_name: str) -> APKPureWebDetail:
        search_html = await self._load_html(f"{self.base_url}/search?q={quote(package_name, safe='')}")
        detail_url = self._search_result_url(search_html, package_name)
        return self._detail_from_html(await self._load_html(detail_url), package_name, detail_url)

    async def _load_html(self, url: str) -> str:
        return await apkpure_versions.load_html(
            url, provider_id=self.id, user_agent=self.user_agent, timeout_seconds=self.timeout_seconds, proxy=self.proxy
        )

    async def _content_disposition(self, url: str | None) -> str | None:
        result = await self._head(url)
        return result[1] if result else None

    async def _head(self, url: str | None) -> tuple[str, str | None] | None:
        return await apkpure_versions.head(
            url, user_agent=self.user_agent, timeout_seconds=self.timeout_seconds, proxy=self.proxy
        )

    async def _constructed_download(self, package_name: str, version_code: int, raw_type: str | None) -> tuple[PackageFileType, str, str | None]:
        try:
            candidates = [self._file_type(raw_type, None, None)]
        except ProviderException:
            candidates = [PackageFileType.BASE_APK, PackageFileType.XAPK, PackageFileType.APKS]

        for file_type in candidates:
            url = self._constructed_url(package_name, version_code, file_type)
            head = await self._head(url)
            if not head:
                if raw_type:
                    return file_type, url, None
                continue
            final_url, content_disposition = head
            if self._file_type(raw_type, final_url, content_disposition) == file_type:
                return file_type, url, content_disposition
        self._fail(ErrorCode.BAD_RESPONSE, "APKPure web constructed download url is unavailable.")

    def _search_result_url(self, html: str, package_name: str) -> str:
        return apkpure_versions.search_result_url(html, self.base_url, package_name, provider_id=self.id)

    def _detail_from_html(self, html: str, package_name: str, detail_url: str) -> APKPureWebDetail:
        parsed = self._parse(html)
        button = self._download_button(parsed.links)
        version_name = self._attr(button, "data-dt-version") or self._regex_attr(html, "data-dt-version")
        version_code = self._int(
            self._attr(button, "data-dt-version_code")
            or self._attr(button, "data-dt-versioncode")
            or self._regex_attr(html, "data-dt-version_code")
            or self._regex_attr(html, "data-dt-versioncode")
        )
        raw_file_type = self._attr(button, "data-dt-filetype") or self._regex_attr(html, "data-dt-filetype")
        href = self._attr(button, "href")
        if not version_name and version_code is None:
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure web detail page is missing version fields.")
        return APKPureWebDetail(
            package_name=package_name,
            app_name=parsed.h1[0] if parsed.h1 else package_name,
            detail_url=detail_url,
            version_name=version_name,
            version_code=version_code,
            raw_file_type=raw_file_type,
            download_page_url=urljoin(detail_url, href) if href else f"{detail_url.rstrip('/')}/download",
        )

    def _download_url_from_html(self, html: str) -> str | None:
        return apkpure_versions.download_url_from_html(html, self.base_url)

    def _download_button(self, links: list[dict[str, str]]) -> dict[str, str]:
        for link in links:
            classes = set((link.get("class") or "").split())
            if classes & {"dt-main-download-btn", "da", "download_apk_news"} or link.get("data-dt-version"):
                return link
        return {}

    def _file_type(self, raw_type: str | None, url: str | None, content_disposition: str | None) -> PackageFileType:
        return apkpure_versions.file_type_from(raw_type, url, content_disposition, provider_id=self.id)

    def _constructed_url(self, package_name: str, version_code: int, file_type: PackageFileType) -> str:
        return apkpure_versions.constructed_url(package_name, version_code, file_type)

    def _file_name(self, file_type: PackageFileType) -> str:
        return apkpure_versions.file_name(file_type)

    def _api_download_url(self, package_name: str, version_code: int | None = None, version_name: str | None = None) -> str:
        params: dict[str, str] = {"provider": self.id}
        if version_code is not None:
            params["versionCode"] = str(version_code)
        elif version_name:
            params["versionName"] = version_name
        return f"/api/v1/android/apps/{quote(package_name, safe='')}/download?{urlencode(params)}"

    def _parse(self, html: str) -> apkpure_versions.LinkParser:
        return apkpure_versions.parse_links(html)

    def _regex_attr(self, html: str, name: str) -> str | None:
        match = re.search(rf"{re.escape(name)}=[\"']([^\"']+)", html, re.IGNORECASE)
        return unescape(match.group(1)) if match else None

    def _attr(self, values: dict[str, str], name: str) -> str | None:
        value = values.get(name)
        return value or None

    def _int(self, value: str | None) -> int | None:
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    def _fail(self, error: ErrorCode, message: str) -> NoReturn:
        raise ProviderException(ProviderError(provider=self.id, error=error, message=message))
