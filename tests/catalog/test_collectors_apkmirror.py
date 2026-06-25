import asyncio

from app.catalog.collectors.apkmirror import APKMirrorCollector
from app.providers import apkmirror_versions
from tests.providers.test_apkmirror_versions import SEARCH_HTML, UPLOADS_HTML

PKG = "com.vitastudio.mahjong"


def _patch(monkeypatch):
    async def fake_load_html(url, **kwargs):
        if "searchtype=apk" in url:
            return SEARCH_HTML
        if "/uploads/" in url:
            return UPLOADS_HTML
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(apkmirror_versions, "load_html", fake_load_html)


def test_collect_lists_versions_with_release_key_and_date(monkeypatch):
    _patch(monkeypatch)
    collector = APKMirrorCollector(user_agent="UA", timeout_seconds=5)

    records = asyncio.run(collector.collect(PKG))
    by_name = {r.version_name: r for r in records}

    assert set(by_name) == {"3.26.0", "3.25.0"}
    assert by_name["3.26.0"].version_code is None  # 列表阶段无 code
    assert by_name["3.26.0"].release_date == "2026-06-22"  # MM/DD/YYYY → YYYY-MM-DD
    key = by_name["3.26.0"].download_key
    assert key["app_slug"] == "vita-mahjong" and key["dev_slug"] == "vita-studio"
    assert key["release_url"].endswith("/vita-mahjong-3-26-0-release/")
