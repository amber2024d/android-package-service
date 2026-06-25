import re

from app.catalog.collectors.base import Collector, VersionRecord
from app.providers import apkmirror_versions

_UTC_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")  # APKMirror data-utcdate: MM/DD/YYYY ...


class APKMirrorCollector(Collector):
    """APKMirror 源采集器：搜索定位 slug → uploads 翻页列全部历史版本（§11.2 实测深度约 4×APKPure）。

    列表阶段 versionCode 拿不到（进变体页 / `.apkm info.json` 才有），先只给 name + date + release_url；
    code 在下载解包后由账本回填权威值。download_key 记 release_url / slug，供 provider 直命中。
    """

    source = "apkmirror"

    def __init__(self, *, user_agent: str, timeout_seconds: float, proxy: str | None = None):
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.proxy = proxy

    async def collect(self, package: str) -> list[VersionRecord]:
        dev_slug, app_slug = await apkmirror_versions.resolve_slugs(
            package,
            provider_id=self.source,
            user_agent=self.user_agent,
            timeout_seconds=self.timeout_seconds,
            proxy=self.proxy,
        )
        versions = await apkmirror_versions.list_versions(
            package,
            app_slug,
            provider_id=self.source,
            user_agent=self.user_agent,
            timeout_seconds=self.timeout_seconds,
            proxy=self.proxy,
        )
        return [
            VersionRecord(
                version_name=version.version_name,
                version_code=None,
                release_date=_normalize_date(version.release_date),
                download_key={"release_url": version.release_url, "dev_slug": dev_slug, "app_slug": app_slug},
            )
            for version in versions
        ]


def _normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    match = _UTC_DATE_RE.search(value)
    if not match:
        return None
    month, day, year = match.groups()
    return f"{year}-{month}-{day}"
