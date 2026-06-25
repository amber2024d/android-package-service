"""版本目录源采集器（阶段 11+）。

把「版本枚举」从 provider 搬到目录这一层：每个采集器对应一个上游源，抓出该源能提供的版本列表
（versionName + 可选 versionCode + 该源稳定下载键），交给 VersionCatalog 聚合入 SQLite。
provider 仍负责下载，本层只灌数据。
"""

from app.catalog.collectors.base import Collector, VersionRecord

__all__ = ["Collector", "VersionRecord"]
