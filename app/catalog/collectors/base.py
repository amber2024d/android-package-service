from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class VersionRecord:
    """采集器产出的单条版本记录。

    - `version_name`：主键（§3.1，目录以 versionName 归并）；缺名的记录采集器应直接丢弃。
    - `version_code`：源带就给，没有就 None。
    - `release_date`：源带就给（如 Aptoide），写入 versions.first/last_seen_date；APKPure 网页不暴露则 None。
    - `download_key`：该源「稳定」下载键（§C），可 JSON 序列化。APKPure→apkid/detail_url；Aptoide→app_id/md5。
    """

    version_name: str
    version_code: int | None = None
    release_date: str | None = None
    download_key: dict | None = None


class Collector(ABC):
    """一个上游源的版本采集器。

    `collect` 取全量；`collect_recent` 取最新优先的增量（默认退化为全量——APKPure/Aptoide 的版本接口
    本就一次返回全集，stop-on-known 由上层按 versionName 去重实现；APKMirror 这类分页源在阶段 15
    覆写 `collect_recent` 做真正的翻页 stop-on-known）。
    """

    source: str

    @abstractmethod
    async def collect(self, package: str) -> list[VersionRecord]:
        ...

    async def collect_recent(self, package: str, cursor: str | None = None) -> list[VersionRecord]:
        return await self.collect(package)
