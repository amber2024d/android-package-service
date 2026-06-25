import asyncio
import base64

from app.catalog.collectors.apkpure import APKPureCollector
from app.providers import apkpure_versions

PKG = "com.oakever.meowdoku"
DETAIL = "https://apkpure.com/meowdoku-brain-puzzle/com.oakever.meowdoku"


def _apkid(pkg: str, code: int, typ: str = "XAPK", digest: str = "deadbeef") -> str:
    token = base64.b64encode(f"{pkg}_{code}_{digest}".encode()).decode()
    return f"b/{typ}/{token}"


def _row(pkg: str, code: int, name: str | None, typ: str = "XAPK") -> str:
    name_attr = f'data-dt-version="{name}" ' if name is not None else ""
    return f'<div {name_attr}data-dt-versioncode="{code}" data-dt-apkid="{_apkid(pkg, code, typ)}">x</div>'


def _versions_html() -> str:
    return "".join(
        [
            _row(PKG, 288, "1.6.0", typ="XAPK"),
            _row(PKG, 116, "1.2.1", typ="APK"),
            _row(PKG, 50, None),  # 没有 versionName -> 采集器丢弃（主键需要 name）
            _row("com.apkpure.aegon", 3207057, "3.20.7005"),  # 推广项 -> parse 阶段按 apkid 过滤
        ]
    )


def _patch_load_html(monkeypatch):
    async def load_html(url, **kwargs):
        if "/search" in url:
            return f'<a href="{DETAIL}">meowdoku</a>'
        if url.endswith("/versions"):
            return _versions_html()
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(apkpure_versions, "load_html", load_html)


def test_collect_returns_records_with_download_key(monkeypatch):
    _patch_load_html(monkeypatch)
    collector = APKPureCollector(user_agent="UA", timeout_seconds=5)

    records = asyncio.run(collector.collect(PKG))
    by_name = {r.version_name: r for r in records}

    assert set(by_name) == {"1.6.0", "1.2.1"}  # 缺 name 与推广项都被排除
    assert by_name["1.6.0"].version_code == 288
    assert by_name["1.2.1"].version_code == 116
    assert by_name["1.6.0"].download_key["file_type"] == "XAPK"
    assert by_name["1.2.1"].download_key["file_type"] == "BASE_APK"
    assert by_name["1.6.0"].download_key["detail_url"] == DETAIL
    assert by_name["1.6.0"].download_key["apkid"].startswith("b/XAPK/")


def test_collect_recent_defaults_to_full(monkeypatch):
    _patch_load_html(monkeypatch)
    collector = APKPureCollector(user_agent="UA", timeout_seconds=5)

    full = asyncio.run(collector.collect(PKG))
    recent = asyncio.run(collector.collect_recent(PKG, cursor=None))
    assert {r.version_name for r in full} == {r.version_name for r in recent}
