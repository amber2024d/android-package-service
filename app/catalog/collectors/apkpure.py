from app.catalog.collectors.base import Collector, VersionRecord
from app.providers import apkpure_versions


class APKPureCollector(Collector):
    """APKPure 源采集器：复用 apkpure_versions 工具（与 provider 共享，本层不下载）。

    流程：搜索页解析出带 slug 的详情页 → 抓 /versions → 解析出全部历史版本
    （name + code + apkid + 文件类型）。download_key 记 apkid / detail_url / file_type，
    供阶段 12 编排器交给 provider 直接命中 /download/{name}。
    """

    source = "apkpure"

    def __init__(self, *, user_agent: str, timeout_seconds: float, proxy: str | None = None, base_url: str | None = None):
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.proxy = proxy
        self.base_url = base_url or apkpure_versions.WEB_BASE_URL

    async def collect(self, package: str) -> list[VersionRecord]:
        detail_url = await apkpure_versions.resolve_detail_url(
            self.base_url,
            package,
            provider_id=self.source,
            user_agent=self.user_agent,
            timeout_seconds=self.timeout_seconds,
            proxy=self.proxy,
        )
        versions = await apkpure_versions.list_versions(
            detail_url,
            package,
            provider_id=self.source,
            user_agent=self.user_agent,
            timeout_seconds=self.timeout_seconds,
            proxy=self.proxy,
        )
        return [self._record(version) for version in versions if version.version_name]

    def _record(self, version: apkpure_versions.APKPureVersion) -> VersionRecord:
        return VersionRecord(
            version_name=version.version_name,
            version_code=version.version_code,
            download_key={
                "apkid": version.apkid,
                "detail_url": version.detail_url,
                "file_type": version.file_type.value,
            },
        )
