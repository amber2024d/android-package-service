import logging
from typing import Any

import httpx

from app.catalog.collectors.base import Collector, VersionRecord
from app.catalog.session.appmagic_session import AppMagicSession

logger = logging.getLogger(__name__)

_RELEASES_URL = "https://appmagic.rocks/api/v2/applications/app-info/releases"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class AppMagicCollector(Collector):
    """AppMagic releases 时间线 → **known 层**（§7）。known-only：只知发布过的 versionName + 日期，
    **无 versionCode、无源可下** → `downloadable=False`、不进对外 `/versions`，只供监控/审计/缺口对账。

    `releases` 是「发布事件」非去重版本：**按 versionName 去重**并保留 `[首次, 末次]` 日期区间。
    依赖 `cf_clearance` + `dashly_auth_token`（§7 风险）：会话不可用即降级返回空、不阻断其它源。
    """

    source = "appmagic"
    downloadable = False
    provides_release_date = True  # 发布时间以 AppMagic 为准

    def __init__(
        self,
        session: AppMagicSession,
        *,
        timeout_seconds: float = 120.0,
        country: str = "US",
        store: int = 1,
    ):
        self.session = session
        self.timeout_seconds = timeout_seconds
        self.country = country
        self.store = store

    async def collect(self, package: str) -> list[VersionRecord]:
        if not self.session.available():
            logger.info("appmagic session unavailable, skip %s", package)
            return []
        payload = await self._request_json(package)
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

    async def _request_json(self, package: str) -> dict[str, Any]:
        body = {"country": self.country, "store": self.store, "storeApplicationID": package}
        headers = {
            "User-Agent": _BROWSER_UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://appmagic.rocks/",
            "Cookie": self.session.cookie_header(),
        }
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(self.timeout_seconds, connect=30.0)
        ) as client:
            response = await client.post(_RELEASES_URL, json=body, headers=headers)
        if response.status_code in (401, 403):
            self.session.invalidate()  # cookie/登录态过期 → 置失效，本轮降级
            raise RuntimeError(f"AppMagic auth failed: HTTP {response.status_code}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("AppMagic returned non-object JSON.")
        return payload


def _merge_dates(current: tuple[str | None, str | None] | None, date: str | None) -> tuple[str | None, str | None]:
    # 日期是 YYYY-MM-DD，字典序即时间序；保留 (最早, 最晚) 的非空日期。
    candidates = [d for d in ((current or (None, None)) + (date,)) if d]
    if not candidates:
        return (None, None)
    return (min(candidates), max(candidates))
