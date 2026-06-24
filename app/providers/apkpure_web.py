from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import re
from typing import NoReturn
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx

from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import (
    AndroidPackageInfo,
    AndroidPackageRequest,
    DownloadPlan,
    PackageFile,
    PackageFileType,
    PackageVersion,
)
from app.providers.base import AndroidPackageProvider


CDN_RE = re.compile(r"https?://[^\s\"'<>]+(?:apkpure\.com/b/|winudf\.com)[^\s\"'<>]*", re.IGNORECASE)
WEB_DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass(frozen=True)
class APKPureWebDetail:
    package_name: str
    app_name: str
    detail_url: str
    version_name: str | None
    version_code: int | None
    raw_file_type: str | None
    download_page_url: str


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.h1: list[str] = []
        self._h1_depth = 0
        self._h1_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a":
            self.links.append(values)
        if tag.lower() == "h1":
            self._h1_depth += 1
            self._h1_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "h1" and self._h1_depth:
            text = " ".join("".join(self._h1_chunks).split())
            if text:
                self.h1.append(text)
            self._h1_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._h1_depth:
            self._h1_chunks.append(data)


class APKPureWebProvider(AndroidPackageProvider):
    id = "apkpure-web"
    base_url = "https://apkpure.com"

    def __init__(self, priority: int = 20, enabled: bool = True, timeout_seconds: float = 120.0, user_agent: str = "AndroidPackageService/0.1.0"):
        self.priority = priority
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        detail = await self._load_detail(request.package_name)
        self._check_version(detail, request)
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
        self._check_version_name(detail, request)
        url = None
        content_disposition = None

        if request.version_code is None or request.version_code == detail.version_code:
            download_html = await self._load_html(detail.download_page_url)
            url = self._download_url_from_html(download_html)
            if url:
                content_disposition = await self._content_disposition(url)

        version_code = request.version_code or detail.version_code
        if not url and version_code is not None:
            file_type, url, content_disposition = await self._constructed_download(
                detail.package_name,
                version_code,
                detail.raw_file_type,
            )

        file_type = self._file_type(detail.raw_file_type, url, content_disposition)
        if not url:
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure web download url is missing.")
        version_name = request.version_name or (detail.version_name if version_code == detail.version_code else None)

        return DownloadPlan(
            package_name=detail.package_name,
            app_name=detail.app_name,
            version_name=version_name,
            version_code=version_code,
            provider=self.id,
            files=[
                PackageFile(
                    type=file_type,
                    name=self._file_name(file_type),
                    url=url,
                    headers={**WEB_DOWNLOAD_HEADERS, "Referer": detail.download_page_url},
                    metadata={"asset.type": file_type.value, "download.fallback": "wget"},
                )
            ],
        )

    async def _load_detail(self, package_name: str) -> APKPureWebDetail:
        search_html = await self._load_html(f"{self.base_url}/search?q={quote(package_name, safe='')}")
        detail_url = self._search_result_url(search_html, package_name)
        return self._detail_from_html(await self._load_html(detail_url), package_name, detail_url)

    async def _load_html(self, url: str) -> str:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    page = await browser.new_page(user_agent=self.user_agent)
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
                    if response and response.status == 404:
                        self._fail(ErrorCode.NOT_FOUND, "APKPure web page not found.")
                    if response and response.status >= 500:
                        self._fail(ErrorCode.NETWORK_ERROR, f"APKPure web returned HTTP {response.status}.")
                    return await page.content()
                finally:
                    await browser.close()
        except ProviderException:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            self._fail(ErrorCode.NETWORK_ERROR, f"APKPure web browser failed: {exc}")

    async def _content_disposition(self, url: str | None) -> str | None:
        result = await self._head(url)
        return result[1] if result else None

    async def _head(self, url: str | None) -> tuple[str, str | None] | None:
        if not url:
            return None
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(self.timeout_seconds, connect=30.0),
                headers={"User-Agent": self.user_agent},
            ) as client:
                response = await client.head(url)
            if response.status_code >= 400:
                return None
            return str(response.url), response.headers.get("content-disposition")
        except httpx.RequestError:
            return None

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
        for link in self._parse(html).links:
            href = link.get("href")
            if not href:
                continue
            url = urljoin(self.base_url, href)
            if urlparse(url).path.rstrip("/").endswith(f"/{package_name}"):
                return url
        self._fail(ErrorCode.NOT_FOUND, "APKPure web search had no exact package match.")

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
        for link in self._parse(html).links:
            href = unescape(link.get("href") or "")
            if "apkpure.com/b/" in href or "winudf.com" in href:
                return urljoin(self.base_url, href)
        match = CDN_RE.search(unescape(html))
        return match.group(0) if match else None

    def _download_button(self, links: list[dict[str, str]]) -> dict[str, str]:
        for link in links:
            classes = set((link.get("class") or "").split())
            if classes & {"dt-main-download-btn", "da", "download_apk_news"} or link.get("data-dt-version"):
                return link
        return {}

    def _file_type(self, raw_type: str | None, url: str | None, content_disposition: str | None) -> PackageFileType:
        for value in (raw_type, url, content_disposition):
            normalized = (value or "").upper()
            if "APKS" in normalized or ".APKS" in normalized:
                return PackageFileType.APKS
            if "XAPK" in normalized or ".XAPK" in normalized:
                return PackageFileType.XAPK
            if "APK" in normalized or ".APK" in normalized:
                return PackageFileType.BASE_APK
        self._fail(ErrorCode.BAD_RESPONSE, "APKPure web file type is missing.")

    def _constructed_url(self, package_name: str, version_code: int, file_type: PackageFileType) -> str:
        folder = {PackageFileType.BASE_APK: "APK", PackageFileType.XAPK: "XAPK", PackageFileType.APKS: "APKS"}[file_type]
        return f"https://d.apkpure.com/b/{folder}/{quote(package_name, safe='')}?versionCode={version_code}"

    def _file_name(self, file_type: PackageFileType) -> str:
        return {
            PackageFileType.BASE_APK: "base.apk",
            PackageFileType.XAPK: "base.xapk",
            PackageFileType.APKS: "base.apks",
        }[file_type]

    def _check_version(self, detail: APKPureWebDetail, request: AndroidPackageRequest) -> None:
        if request.version_code is not None and detail.version_code != request.version_code:
            self._fail(ErrorCode.UNSUPPORTED, "APKPure web package info only supports latest versionCode.")
        self._check_version_name(detail, request)

    def _check_version_name(self, detail: APKPureWebDetail, request: AndroidPackageRequest) -> None:
        if request.version_name is not None and detail.version_name != request.version_name:
            self._fail(ErrorCode.UNSUPPORTED, "APKPure web only supports parsed latest versionName.")

    def _api_download_url(self, package_name: str, version_code: int | None = None, version_name: str | None = None) -> str:
        params: dict[str, str] = {"provider": self.id}
        if version_code is not None:
            params["versionCode"] = str(version_code)
        elif version_name:
            params["versionName"] = version_name
        return f"/api/v1/android/apps/{quote(package_name, safe='')}/download?{urlencode(params)}"

    def _parse(self, html: str) -> LinkParser:
        parser = LinkParser()
        parser.feed(html)
        return parser

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
