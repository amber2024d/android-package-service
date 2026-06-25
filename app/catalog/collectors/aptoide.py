import re
from typing import Any
from urllib.parse import quote

import httpx

from app.catalog.collectors.base import Collector, VersionRecord

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class AptoideCollector(Collector):
    """Aptoide 源采集器：`app/get` 的 versions 列表（+ 当前 meta 版本）。

    自带精简抓取/解析（不依赖 AptoideProvider，枚举归位到本层）。download_key 记 app_id / md5，
    供阶段 12 用 app_id 或 apk_md5sum 现查下载。HTTP 走 `_request_json`，测试可直接替换该 seam。
    """

    source = "aptoide"
    base_url = "https://ws75.aptoide.com/api/7/"

    def __init__(self, *, timeout_seconds: float = 120.0, user_agent: str = "AndroidPackageService/0.1.0"):
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    async def collect(self, package: str) -> list[VersionRecord]:
        payload = await self._request_json(f"app/get/package_name={quote(package, safe='')}/aab=1")
        return self._records(payload)

    def _records(self, payload: dict[str, Any]) -> list[VersionRecord]:
        nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
        meta = nodes.get("meta", {}).get("data") if isinstance(nodes.get("meta"), dict) else None
        version_items = nodes.get("versions", {}).get("list") if isinstance(nodes.get("versions"), dict) else None

        records: list[VersionRecord] = []
        seen: set[str] = set()
        # meta（当前/最新版）优先，再叠历史版本列表；按 versionName 去重。
        for item in [meta, *(version_items or [])]:
            record = self._record(item)
            if record is None or record.version_name in seen:
                continue
            seen.add(record.version_name)
            records.append(record)
        return records

    def _record(self, item: Any) -> VersionRecord | None:
        if not isinstance(item, dict):
            return None
        file_data = item.get("file") if isinstance(item.get("file"), dict) else {}
        version_name = file_data.get("vername")
        if not isinstance(version_name, str) or not version_name:
            return None
        download_key = {"app_id": item.get("id"), "md5": file_data.get("md5sum")}
        return VersionRecord(
            version_name=version_name,
            version_code=_int(file_data.get("vercode")),
            release_date=_date(file_data.get("added") or file_data.get("updated")),
            download_key={key: value for key, value in download_key.items() if value is not None},
        )

    async def _request_json(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            follow_redirects=True,
            timeout=httpx.Timeout(self.timeout_seconds, connect=30.0),
            headers={"User-Agent": self.user_agent},
        ) as client:
            response = await client.get(path)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Aptoide returned non-object JSON.")
        return payload


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _DATE_RE.search(value)
    return match.group(0) if match else None
