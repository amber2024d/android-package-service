"""Shared APKMirror web-scraping toolkit (provider-agnostic).

APKMirror 作为更深的 downloadable 源（见版本目录设计 §11.2 实测）。所有 HTML 页都过 Cloudflare，
统一复用 ``apkpure_versions.load_html`` 的无头 Chromium 加载器（接受 ``provider_id``）。流程（§3）：

1. 搜索定位 app slug（dev-slug / app-slug），按包名匹配（结果行图标文件名内嵌包名）。
2. ``/uploads/`` 翻页列版本（app 主页只列最近 10 个，必须翻页才全）→ [(versionName, date, releaseUrl)...]。
3. release 页列变体（arch/dpi/min-sdk，BUNDLE 标记）→ 选变体（优先 universal bundle）。
4. 变体下载页：``Version: {name} ({code})`` + downloadButton ``/download/?key=K1``。
5. 4 跳取直链：``/download/?key=K1``（Playwright）→ ``download.php?id&key=K2``（httpx 跟随 302 到 R2）。

错误按调用方传入的 ``provider_id`` 归属。本模块只做加载+解析，下载/解包在下载层。
"""

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote, urljoin

from app.domain.errors import ErrorCode
from app.providers.apkpure_versions import fail, head, load_html  # 复用通用加载器/错误助手

WEB_BASE_URL = "https://www.apkmirror.com"

_RELEASE_RE = re.compile(r"/apk/[a-z0-9.-]+/[a-z0-9.-]+/[a-z0-9.-]+-release/")
_FONTBLACK_RE = re.compile(r'<a[^>]*class="fontBlack"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', re.IGNORECASE)
_APPROW_RE = re.compile(r'<div class="appRow">')
_UTCDATE_RE = re.compile(r'data-utcdate="([^"]+)"')
_TOTAL_PAGES_RE = re.compile(r"Page\s+\d+\s+of\s+(\d+)", re.IGNORECASE)
_VARIANT_RE = re.compile(r'href="((?:https?://[^"]+)?/apk/[^"]*-android-apk-download/)"', re.IGNORECASE)
_VERSION_CODE_RE = re.compile(r"Version:\s*([0-9A-Za-z][0-9A-Za-z.\-]*)\s*\((\d+)\)")
_DOWNLOAD_BUTTON_RE = re.compile(r'<a[^>]*class="[^"]*downloadButton[^"]*"[^>]*href="([^"]+)"', re.IGNORECASE)
_DOWNLOAD_PHP_RE = re.compile(r'href="([^"]*download\.php\?[^"]+)"', re.IGNORECASE)


@dataclass(frozen=True)
class APKMirrorVersion:
    package_name: str
    version_name: str
    release_url: str
    release_date: str | None = None
    version_code: int | None = None  # 列表页拿不到；进变体页 / .apkm info.json 才有


@dataclass(frozen=True)
class APKMirrorVariant:
    download_page_url: str  # .../{...}-android-apk-download/
    is_bundle: bool = False


@dataclass(frozen=True)
class APKMirrorDownload:
    version_name: str | None
    version_code: int | None
    intermediate_url: str  # 变体下载页的 /download/?key=K1（Playwright 再跳）


# --------------------------------------------------------------------------- #
# 纯解析（离线可测）
# --------------------------------------------------------------------------- #


def parse_app_slug(html: str, package_name: str, *, provider_id: str) -> tuple[str, str]:
    """从搜索结果页解析目标 app 的 (dev_slug, app_slug)。

    优先匹配「图标/区块内嵌了该包名」的结果行；匹配不到时退回第一条 release 链接（APKMirror 对
    全包名搜索通常把精确匹配排首位）。
    """
    blocks = _split_app_rows(html)
    for block in blocks:
        if package_name in block:
            slug = _slug_from_block(block)
            if slug:
                return slug
    for block in blocks:
        slug = _slug_from_block(block)
        if slug:
            return slug
    fail(provider_id, ErrorCode.NOT_FOUND, "APKMirror search had no matching app.")


def parse_uploads_versions(html: str, package_name: str, app_slug: str) -> list[APKMirrorVersion]:
    """解析 uploads 页的一页版本行。按 versionName 去重（页内偶有重复行）。"""
    versions: list[APKMirrorVersion] = []
    seen: set[str] = set()
    prefix = f"/{app_slug}/"
    for block in _split_app_rows(html):
        match = _FONTBLACK_RE.search(block)
        if not match:
            continue
        href, text = match.group(1), unescape(match.group(2)).strip()
        release_url = urljoin(WEB_BASE_URL, href)
        if not _RELEASE_RE.search(release_url) or prefix not in release_url:
            continue
        version_name = text.split()[-1] if text.split() else None
        if not version_name or version_name in seen:
            continue
        seen.add(version_name)
        date_match = _UTCDATE_RE.search(block)
        versions.append(
            APKMirrorVersion(
                package_name=package_name,
                version_name=version_name,
                release_url=release_url,
                release_date=date_match.group(1) if date_match else None,
            )
        )
    return versions


def parse_total_pages(html: str) -> int:
    match = _TOTAL_PAGES_RE.search(html)
    return int(match.group(1)) if match else 1


def parse_release_variants(html: str) -> list[APKMirrorVariant]:
    """解析 release 页的变体下载链接 + 是否 BUNDLE。"""
    variants: list[APKMirrorVariant] = []
    seen: set[str] = set()
    for match in _VARIANT_RE.finditer(html):
        url = urljoin(WEB_BASE_URL, unescape(match.group(1)))
        if url in seen:
            continue
        seen.add(url)
        # BUNDLE 徽章/文本就近出现即视为 bundle 变体（vita-mahjong 单变体即 BUNDLE）。
        window = html[max(0, match.start() - 600) : match.end() + 600]
        is_bundle = "apkm-badge" in window.lower() or "bundle" in window.lower()
        variants.append(APKMirrorVariant(download_page_url=url, is_bundle=is_bundle))
    return variants


def select_variant(variants: list[APKMirrorVariant]) -> APKMirrorVariant | None:
    """优先 universal/BUNDLE 变体（一个文件含全部 split，最接近可直接装）；否则取第一个。"""
    if not variants:
        return None
    for variant in variants:
        if variant.is_bundle:
            return variant
    return variants[0]


def parse_download_page(html: str, *, provider_id: str) -> APKMirrorDownload:
    """变体下载页：取 ``Version: name (code)`` + downloadButton 的 ``/download/?key=K1``。"""
    version_name: str | None = None
    version_code: int | None = None
    version_match = _VERSION_CODE_RE.search(html)
    if version_match:
        version_name = version_match.group(1)
        version_code = int(version_match.group(2))
    button = _DOWNLOAD_BUTTON_RE.search(html)
    if not button:
        fail(provider_id, ErrorCode.BAD_RESPONSE, "APKMirror download button is missing.")
    return APKMirrorDownload(
        version_name=version_name,
        version_code=version_code,
        intermediate_url=urljoin(WEB_BASE_URL, unescape(button.group(1))),
    )


def parse_intermediate_url(html: str, *, provider_id: str) -> str:
    """中间页：取 ``download.php?id&key=K2`` 绝对链接（httpx 跟随 302 到 R2）。"""
    match = _DOWNLOAD_PHP_RE.search(html)
    if not match:
        fail(provider_id, ErrorCode.BAD_RESPONSE, "APKMirror final download link is missing.")
    return urljoin(WEB_BASE_URL, unescape(match.group(1)))


def construct_release_url(dev_slug: str, app_slug: str, version_name: str) -> str:
    """拼 release 页 URL（快路径，省 uploads 翻页）：版本名点转横线。"""
    dashed = version_name.replace(".", "-")
    return f"{WEB_BASE_URL}/apk/{dev_slug}/{app_slug}/{app_slug}-{dashed}-release/"


def _split_app_rows(html: str) -> list[str]:
    starts = [m.start() for m in _APPROW_RE.finditer(html)]
    if not starts:
        return [html]
    bounds = starts + [len(html)]
    return [html[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def _slug_from_block(block: str) -> tuple[str, str] | None:
    match = _RELEASE_RE.search(block)
    if not match:
        return None
    parts = match.group(0).strip("/").split("/")  # apk / dev / app / {..}-release
    if len(parts) >= 3:
        return parts[1], parts[2]
    return None


# --------------------------------------------------------------------------- #
# 异步加载（复用 apkpure_versions 的 Playwright 加载器）
# --------------------------------------------------------------------------- #


async def _load(url: str, *, provider_id: str, user_agent: str, timeout_seconds: float, proxy: str | None) -> str:
    return await load_html(url, provider_id=provider_id, user_agent=user_agent, timeout_seconds=timeout_seconds, proxy=proxy)


async def resolve_slugs(
    package_name: str, *, provider_id: str, user_agent: str, timeout_seconds: float, proxy: str | None = None
) -> tuple[str, str]:
    search_url = f"{WEB_BASE_URL}/?post_type=app_release&searchtype=apk&s={quote(package_name, safe='')}"
    html = await _load(search_url, provider_id=provider_id, user_agent=user_agent, timeout_seconds=timeout_seconds, proxy=proxy)
    return parse_app_slug(html, package_name, provider_id=provider_id)


async def list_versions(
    package_name: str,
    app_slug: str,
    *,
    provider_id: str,
    user_agent: str,
    timeout_seconds: float,
    proxy: str | None = None,
    max_pages: int = 20,
) -> list[APKMirrorVersion]:
    """翻 uploads 全部页，按 versionName 聚合去重。"""
    first_url = f"{WEB_BASE_URL}/uploads/?appcategory={quote(app_slug, safe='')}"
    first_html = await _load(first_url, provider_id=provider_id, user_agent=user_agent, timeout_seconds=timeout_seconds, proxy=proxy)
    total = min(parse_total_pages(first_html), max_pages)

    collected: dict[str, APKMirrorVersion] = {}
    for version in parse_uploads_versions(first_html, package_name, app_slug):
        collected.setdefault(version.version_name, version)
    for page in range(2, total + 1):
        page_url = f"{WEB_BASE_URL}/uploads/page/{page}/?appcategory={quote(app_slug, safe='')}"
        page_html = await _load(page_url, provider_id=provider_id, user_agent=user_agent, timeout_seconds=timeout_seconds, proxy=proxy)
        for version in parse_uploads_versions(page_html, package_name, app_slug):
            collected.setdefault(version.version_name, version)
    return list(collected.values())


async def resolve_download_url(
    release_url: str,
    *,
    provider_id: str,
    user_agent: str,
    timeout_seconds: float,
    proxy: str | None = None,
) -> tuple[str, APKMirrorDownload]:
    """release 页 → 选变体 → 变体下载页 → 中间页 → 返回 (download.php 绝对链接, 下载页字段)。"""
    release_html = await _load(release_url, provider_id=provider_id, user_agent=user_agent, timeout_seconds=timeout_seconds, proxy=proxy)
    variant = select_variant(parse_release_variants(release_html))
    if variant is None:
        fail(provider_id, ErrorCode.NOT_FOUND, "APKMirror release has no downloadable variant.")

    download_html = await _load(
        variant.download_page_url, provider_id=provider_id, user_agent=user_agent, timeout_seconds=timeout_seconds, proxy=proxy
    )
    download = parse_download_page(download_html, provider_id=provider_id)

    intermediate_html = await _load(
        download.intermediate_url, provider_id=provider_id, user_agent=user_agent, timeout_seconds=timeout_seconds, proxy=proxy
    )
    final_url = parse_intermediate_url(intermediate_html, provider_id=provider_id)
    return final_url, download
