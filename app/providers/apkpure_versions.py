"""Shared APKPure web-scraping toolkit.

APKPure 的签名移动 API（``get_app_detail``）只返回最新版，历史版本只能靠网页抓取：

1. 通过搜索页解析出带 slug 的详情页 URL（``/{slug}/{package}``）。
2. 抓 ``/{slug}/{package}/versions`` 页，解析出全部历史版本（versionName /
   versionCode / apkid / 文件类型）。
3. 抓某个版本的下载页 ``/{slug}/{package}/download/{versionName}``，从里面提取
   预签名的 ``d.apkpure.com/custom/...`` CDN 链接；拿不到时回退到 apkid 构造的
   ``d.apkpure.com/{apkid}`` 链接。

这些页面都过 Cloudflare，统一用无头 Chromium（Playwright）加载。本模块对 provider
无关：错误按调用方传入的 ``provider_id`` 归属，``apkpure-web`` 和 ``apkpure-signed``
共用同一套实现。
"""

import base64
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any, NoReturn
from urllib.parse import quote, unquote, urljoin, urlparse, urlsplit

import httpx

from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import PackageFile, PackageFileType


WEB_BASE_URL = "https://apkpure.com"
CDN_BASE_URL = "https://d.apkpure.com"

CDN_RE = re.compile(r"https?://[^\s\"'<>]+(?:apkpure\.com/b/|winudf\.com)[^\s\"'<>]*", re.IGNORECASE)

# 桌面浏览器请求头：下载预签名 CDN 链接时复用，wget 兜底也会带上。
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

_FILE_TYPE_NAMES = {
    PackageFileType.BASE_APK: "base.apk",
    PackageFileType.XAPK: "base.xapk",
    PackageFileType.APKS: "base.apks",
}
_APKID_TYPE = {"APK": PackageFileType.BASE_APK, "XAPK": PackageFileType.XAPK, "APKS": PackageFileType.APKS}
# /versions 页每个版本项携带 data-dt-versioncode（无下划线）；详情页的下载按钮用的是
# data-dt-version_code（有下划线），靠属性名差异天然避免把详情按钮当成版本项。
_VERSION_ROW_RE = re.compile(r"<(?:div|a|li)\b[^>]*\bdata-dt-versioncode=\"\d+\"[^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class APKPureVersion:
    package_name: str
    version_name: str | None
    version_code: int | None
    apkid: str  # 形如 "b/XAPK/<base64>"
    file_type: PackageFileType
    detail_url: str

    @property
    def download_page_url(self) -> str:
        # 历史版本下载页：/{slug}/{package}/download/{versionName}
        suffix = quote(self.version_name, safe=".-_") if self.version_name else str(self.version_code)
        return f"{self.detail_url.rstrip('/')}/download/{suffix}"

    @property
    def cdn_apkid_url(self) -> str:
        return f"{CDN_BASE_URL}/{self.apkid.lstrip('/')}"


class LinkParser(HTMLParser):
    """提取所有 <a> 标签属性，以及 <h1> 文本（用于解析应用名）。"""

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


def parse_links(html: str) -> LinkParser:
    parser = LinkParser()
    parser.feed(html)
    return parser


def chromium_proxy(proxy_url: str | None) -> dict[str, str] | None:
    """把 ``http://user:pass@host:port`` 拆成 Chromium launch 需要的 proxy 字典。"""
    if not proxy_url:
        return None
    parts = urlsplit(proxy_url)
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server = f"{server}:{parts.port}"
    proxy: dict[str, str] = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


async def load_html(
    url: str, *, provider_id: str, user_agent: str, timeout_seconds: float, proxy: str | None = None
) -> str:
    """用无头 Chromium 打开页面并返回渲染后的 HTML（绕过 Cloudflare）。"""
    launch_kwargs: dict[str, Any] = {"headless": True, "args": ["--no-sandbox"]}
    chromium = chromium_proxy(proxy)
    if chromium:
        launch_kwargs["proxy"] = chromium
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                page = await browser.new_page(user_agent=user_agent)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                if response and response.status == 404:
                    fail(provider_id, ErrorCode.NOT_FOUND, "APKPure web page not found.")
                if response and response.status >= 500:
                    fail(provider_id, ErrorCode.NETWORK_ERROR, f"APKPure web returned HTTP {response.status}.")
                return await page.content()
            finally:
                await browser.close()
    except ProviderException:
        raise
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        fail(provider_id, ErrorCode.NETWORK_ERROR, f"APKPure web browser failed: {exc}")


async def head(
    url: str | None, *, user_agent: str, timeout_seconds: float, proxy: str | None = None
) -> tuple[str, str | None] | None:
    """HEAD 探测：返回 (最终 URL, content-disposition)；失败返回 None。"""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds, connect=30.0),
            headers={"User-Agent": user_agent},
            proxy=proxy,
        ) as client:
            response = await client.head(url)
        if response.status_code >= 400:
            return None
        return str(response.url), response.headers.get("content-disposition")
    except httpx.RequestError:
        return None


def search_result_url(html: str, base_url: str, package_name: str, *, provider_id: str) -> str:
    """从搜索结果页里找到与包名精确匹配的详情页 URL（带 slug）。"""
    for link in parse_links(html).links:
        href = link.get("href")
        if not href:
            continue
        url = urljoin(base_url, href)
        if urlparse(url).path.rstrip("/").endswith(f"/{package_name}"):
            return url
    fail(provider_id, ErrorCode.NOT_FOUND, "APKPure web search had no exact package match.")


def _is_installer_stub(href: str, css_class: str) -> bool:
    """识别 APKPure「一键安装器」壳：下载页第一个按钮（class=fast-download-start-btn）指向
    ``/custom/com.apkpure.aegon-*.apk``，那是个 ~6MB 的 APKPure 安装器、包名是 APKPure 自己的
    ``com.apkpure.aegon``，跑起来才去拉真包，绝不是目标 app。XAPK 应用尤其会被它顶成一个
    BASE_APK 安装器壳（见 com.mintgames.findout 1.0.17），必须跳过它取后面真正的下载按钮。"""
    return "com.apkpure.aegon" in href or "fast-download" in css_class.lower()


def download_url_from_html(html: str, base_url: str) -> str | None:
    """从下载页里提取真实的 CDN 下载链接（winudf / apkpure 的 /b/ 或 /custom/）。

    跳过 APKPure 安装器壳（见 ``_is_installer_stub``）：它的 ``/custom/...apk`` 链接排在真链
    （``download-start-btn`` → ``/b/XAPK|APK|APKS/...``）前面，不过滤会把 XAPK 误判成 BASE_APK。
    """
    for link in parse_links(html).links:
        href = unescape(link.get("href") or "")
        if not ("apkpure.com/b/" in href or "apkpure.com/custom/" in href or "winudf.com" in href):
            continue
        if _is_installer_stub(href, link.get("class") or ""):
            continue
        return urljoin(base_url, href)
    match = CDN_RE.search(unescape(html))
    return match.group(0) if match else None


def file_type_from(*candidates: str | None, provider_id: str, default: PackageFileType | None = None) -> PackageFileType:
    """按候选串（依传入顺序）推断包类型；都不匹配时回退 default，否则报错。"""
    for value in candidates:
        normalized = (value or "").upper()
        if "APKS" in normalized or ".APKS" in normalized:
            return PackageFileType.APKS
        if "XAPK" in normalized or ".XAPK" in normalized:
            return PackageFileType.XAPK
        if "APK" in normalized or ".APK" in normalized:
            return PackageFileType.BASE_APK
    if default is not None:
        return default
    fail(provider_id, ErrorCode.BAD_RESPONSE, "APKPure web file type is missing.")


def file_name(file_type: PackageFileType) -> str:
    return _FILE_TYPE_NAMES.get(file_type, "base.apk")


def constructed_url(package_name: str, version_code: int, file_type: PackageFileType) -> str:
    folder = {PackageFileType.BASE_APK: "APK", PackageFileType.XAPK: "XAPK", PackageFileType.APKS: "APKS"}[file_type]
    return f"{CDN_BASE_URL}/b/{folder}/{quote(package_name, safe='')}?versionCode={version_code}"


def _decode_apkid_package(apkid: str) -> str | None:
    """apkid 形如 ``b/XAPK/<base64>``，base64 解码后是 ``{package}_{versionCode}_{hash}``。"""
    parts = apkid.split("/")
    if len(parts) < 3 or not parts[-1]:
        return None
    token = parts[-1]
    try:
        decoded = base64.b64decode(token + "=" * (-len(token) % 4)).decode("utf-8", "ignore")
    except (ValueError, base64.binascii.Error):
        return None
    return decoded.rsplit("_", 2)[0] if "_" in decoded else decoded


def _attr(tag: str, name: str) -> str | None:
    match = re.search(rf'{re.escape(name)}="([^"]*)"', tag, re.IGNORECASE)
    return unescape(match.group(1)) if match and match.group(1) else None


def parse_versions(html: str, package_name: str, detail_url: str) -> list[APKPureVersion]:
    """解析 /versions 页，仅保留属于目标包的版本项（按 apkid 解码出的包名过滤）。

    页面里会混入「相关应用」「APKPure 客户端」等推广项，它们的 apkid 解码后是别的包名，
    据此过滤掉；同一 versionCode 只保留首次出现的一条。
    """
    versions: list[APKPureVersion] = []
    seen: set[int | None] = set()
    for tag in _VERSION_ROW_RE.findall(html):
        apkid = _attr(tag, "data-dt-apkid") or ""
        if not apkid.startswith("b/"):
            continue
        if _decode_apkid_package(apkid) != package_name:
            continue
        version_code = _attr(tag, "data-dt-versioncode")
        code = int(version_code) if version_code and version_code.isdigit() else None
        if code in seen:
            continue
        seen.add(code)
        apkid_type = apkid.split("/")[1].upper() if "/" in apkid else ""
        versions.append(
            APKPureVersion(
                package_name=package_name,
                version_name=_attr(tag, "data-dt-version"),
                version_code=code,
                apkid=apkid,
                file_type=_APKID_TYPE.get(apkid_type, PackageFileType.BASE_APK),
                detail_url=detail_url,
            )
        )
    return versions


def select_version(
    versions: list[APKPureVersion],
    *,
    version_code: int | None,
    version_name: str | None,
) -> APKPureVersion | None:
    """按 versionCode 优先、其次 versionName 选中目标版本。"""
    for version in versions:
        if version_code is not None and version.version_code == version_code:
            return version
    if version_code is not None:
        return None
    for version in versions:
        if version_name is not None and version.version_name == version_name:
            return version
    return None


async def resolve_detail_url(
    base_url: str, package_name: str, *, provider_id: str, user_agent: str, timeout_seconds: float, proxy: str | None = None
) -> str:
    search_html = await load_html(
        f"{base_url}/search?q={quote(package_name, safe='')}",
        provider_id=provider_id,
        user_agent=user_agent,
        timeout_seconds=timeout_seconds,
        proxy=proxy,
    )
    return search_result_url(search_html, base_url, package_name, provider_id=provider_id)


async def list_versions(
    detail_url: str, package_name: str, *, provider_id: str, user_agent: str, timeout_seconds: float, proxy: str | None = None
) -> list[APKPureVersion]:
    html = await load_html(
        f"{detail_url.rstrip('/')}/versions",
        provider_id=provider_id,
        user_agent=user_agent,
        timeout_seconds=timeout_seconds,
        proxy=proxy,
    )
    return parse_versions(html, package_name, detail_url)


async def resolve_version_file(
    version: APKPureVersion, *, provider_id: str, user_agent: str, timeout_seconds: float, proxy: str | None = None
) -> PackageFile:
    """把某个历史版本解析成可下载的 PackageFile（预签名 CDN 链接，apkid 兜底）。"""
    download_page = version.download_page_url
    html = await load_html(
        download_page, provider_id=provider_id, user_agent=user_agent, timeout_seconds=timeout_seconds, proxy=proxy
    )
    url = download_url_from_html(html, WEB_BASE_URL)

    content_disposition: str | None = None
    if url:
        probe = await head(
            url, user_agent=WEB_DOWNLOAD_HEADERS["User-Agent"], timeout_seconds=timeout_seconds, proxy=proxy
        )
        content_disposition = probe[1] if probe else None
        # 实际下载文件的扩展名/Content-Disposition 优先于 apkid 标注的类型。
        file_type = file_type_from(url, content_disposition, provider_id=provider_id, default=version.file_type)
    else:
        # 拿不到预签名链接时回退到 apkid 构造的 CDN 链接，类型沿用 apkid 标注。
        url = version.cdn_apkid_url
        file_type = version.file_type

    return PackageFile(
        type=file_type,
        name=file_name(file_type),
        url=url,
        size=None,
        headers={**WEB_DOWNLOAD_HEADERS, "Referer": download_page},
        proxy=proxy,
        metadata={"asset.type": file_type.value, "download.fallback": "wget", "source.versions_page": "1"},
    )


def fail(provider_id: str, error: ErrorCode, message: str) -> NoReturn:
    raise ProviderException(ProviderError(provider=provider_id, error=error, message=message))
