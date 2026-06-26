import json
import logging
from typing import Any

import httpx

from app.catalog.collectors.base import Collector, VersionRecord
from app.providers.apkpure_versions import chromium_proxy

logger = logging.getLogger(__name__)

_RELEASES_URL = "https://appmagic.rocks/api/v2/applications/app-info/releases"
_HOME_URL = "https://appmagic.rocks/"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 在 appmagic.rocks 页面上下文里 POST releases 接口：同源 + 浏览器已解 Cloudflare 挑战。
_FETCH_JS = """
async (args) => {
    const [url, body] = args;
    const r = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
        body: JSON.stringify(body),
        credentials: 'include',
    });
    return {status: r.status, body: await r.text()};
}
"""


class AppMagicCollector(Collector):
    """AppMagic releases 时间线 → **known 层**（§7）。known-only：只知发布过的 versionName + 日期，
    **无 versionCode、无源可下** → `downloadable=False`、不进对外 `/versions`，只供监控/审计/缺口对账。

    `releases` 是「发布事件」非去重版本：**按 versionName 去重**并保留 `[首次, 末次]` 日期区间。
    取数分两层：默认匿名 httpx（实测接口公开、Cloudflare 不挑战、无需登录）；httpx 被 Cloudflare 拦（机房 IP
    更易遇到）则回退无头 Chromium 在页面上下文里 `fetch`（真实浏览器自动解挑战）。
    """

    source = "appmagic"
    downloadable = False
    provides_release_date = True  # 发布时间以 AppMagic 为准

    def __init__(
        self,
        *,
        timeout_seconds: float = 120.0,
        country: str = "US",
        store: int = 1,
        proxy: str | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.country = country
        self.store = store
        self.proxy = proxy

    async def collect(self, package: str) -> list[VersionRecord]:
        payload = await self._fetch(package)
        return self._records(payload)

    def _records(self, payload: Any) -> list[VersionRecord]:
        releases = payload.get("releases") if isinstance(payload, dict) else None
        if not isinstance(releases, list):
            return []
        ranges: dict[str, tuple[str | None, str | None]] = {}
        for item in releases:
            if not isinstance(item, dict):
                continue
            name = item.get("version")
            if not isinstance(name, str) or not name:
                continue
            date = item.get("release_date")
            date = date if isinstance(date, str) and date else None
            ranges[name] = _merge_dates(ranges.get(name), date)
        return [
            VersionRecord(
                version_name=name,
                version_code=None,  # AppMagic 没有 versionCode
                release_date=first,
                last_release_date=last,
                download_key={"release_date": first} if first else {},
            )
            for name, (first, last) in ranges.items()
        ]

    async def _fetch(self, package: str) -> dict[str, Any]:
        # 1) 匿名 httpx——绝大多数情况够用。
        try:
            payload = await self._fetch_httpx(package)
            if payload is not None:
                return payload
        except httpx.HTTPError as exc:
            logger.info("appmagic httpx error (%s), fallback to playwright: %s", exc, package)
        # 2) httpx 被 Cloudflare 拦或网络异常 → 无头浏览器兜底（解 JS 挑战）。
        return await self._fetch_browser(package)

    async def _fetch_httpx(self, package: str) -> dict[str, Any] | None:
        body = {"country": self.country, "store": self.store, "storeApplicationID": package}
        headers = {
            "User-Agent": _BROWSER_UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": _HOME_URL,
        }
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(self.timeout_seconds, connect=30.0), proxy=self.proxy
        ) as client:
            response = await client.post(_RELEASES_URL, json=body, headers=headers)
        if self._looks_blocked(response):
            logger.info(
                "appmagic httpx blocked (HTTP %s), fallback to playwright: %s", response.status_code, package
            )
            return None  # 让 _fetch 走浏览器兜底
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("AppMagic returned non-object JSON.")
        return payload

    async def _fetch_browser(self, package: str) -> dict[str, Any]:
        from playwright.async_api import async_playwright

        body = {"country": self.country, "store": self.store, "storeApplicationID": package}
        launch_kwargs: dict[str, Any] = {"headless": True, "args": ["--no-sandbox"]}
        chromium = chromium_proxy(self.proxy)
        if chromium:
            launch_kwargs["proxy"] = chromium
        timeout_ms = self.timeout_seconds * 1000
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                page = await browser.new_page()
                # 先进 appmagic.rocks 建立同源上下文并让浏览器解 Cloudflare 挑战，再发同源 fetch。
                await page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                result = await page.evaluate(_FETCH_JS, [_RELEASES_URL, body])
            finally:
                await browser.close()
        if not isinstance(result, dict) or result.get("status") != 200:
            status = result.get("status") if isinstance(result, dict) else None
            raise RuntimeError(f"AppMagic browser fetch failed: HTTP {status}")
        payload = json.loads(result["body"])
        if not isinstance(payload, dict):
            raise ValueError("AppMagic returned non-object JSON.")
        return payload

    @staticmethod
    def _looks_blocked(response: httpx.Response) -> bool:
        # Cloudflare 挑战/限流：403/429/503 或返回 HTML 挑战页（非 JSON）。
        if response.status_code in (403, 429, 503):
            return True
        return "json" not in response.headers.get("content-type", "").lower()


def _merge_dates(current: tuple[str | None, str | None] | None, date: str | None) -> tuple[str | None, str | None]:
    # 日期是 YYYY-MM-DD，字典序即时间序；保留 (最早, 最晚) 的非空日期。
    candidates = [d for d in ((current or (None, None)) + (date,)) if d]
    if not candidates:
        return (None, None)
    return (min(candidates), max(candidates))
