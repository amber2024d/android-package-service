import asyncio

from app.catalog.collectors.appmagic import AppMagicCollector


def _payload() -> dict:
    return {
        "releases": [
            {"release_date": "2024-03-22", "version": "1.6.0", "release_notes": ""},
            {"release_date": "2024-03-25", "version": "1.6.0"},  # 同名后发事件 → 末次日期
            {"release_date": "2024-01-10", "version": "1.5.0"},
            {"release_date": None, "version": "1.4.0"},  # 无日期
            {"version": "1.4.0"},  # 同名重复
            {"release_date": "2024-02-01"},  # 无 version → 丢
            "garbage",
        ]
    }


def _collector() -> AppMagicCollector:
    collector = AppMagicCollector()

    async def fake_fetch(package: str) -> dict:
        return _payload()

    collector._fetch = fake_fetch
    return collector


def test_is_known_only_source():
    collector = AppMagicCollector()
    assert collector.downloadable is False
    assert collector.source == "appmagic"
    assert collector.provides_release_date is True  # 发布时间以 AppMagic 为准


def test_records_dedup_by_name_with_first_last_dates():
    records = asyncio.run(_collector().collect("com.pkg"))
    by_name = {r.version_name: r for r in records}

    assert set(by_name) == {"1.6.0", "1.5.0", "1.4.0"}
    assert by_name["1.6.0"].version_code is None  # known-only：无 code
    assert by_name["1.6.0"].release_date == "2024-03-22"  # 首次
    assert by_name["1.6.0"].last_release_date == "2024-03-25"  # 末次
    assert by_name["1.5.0"].release_date == "2024-01-10"
    assert by_name["1.4.0"].release_date is None  # 无日期容忍
    assert by_name["1.6.0"].download_key == {"release_date": "2024-03-22"}


def test_fetch_is_anonymous():
    # 接口公开匿名可取：直接取数，不依赖任何 cookie/会话。
    records = asyncio.run(_collector().collect("com.pkg"))
    assert {r.version_name for r in records} == {"1.6.0", "1.5.0", "1.4.0"}


def test_cloudflare_block_falls_back_to_browser():
    collector = AppMagicCollector()
    used = {"browser": False}

    async def httpx_blocked(package):
        return None  # 模拟被 Cloudflare 拦

    async def browser_ok(package):
        used["browser"] = True
        return _payload()

    collector._fetch_httpx = httpx_blocked
    collector._fetch_browser = browser_ok
    records = asyncio.run(collector.collect("com.pkg"))
    assert used["browser"] is True
    assert {r.version_name for r in records} == {"1.6.0", "1.5.0", "1.4.0"}


def test_looks_blocked_detects_cloudflare():
    import httpx

    blocked = httpx.Response(403, headers={"content-type": "text/html"})
    html_200 = httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"})
    ok = httpx.Response(200, headers={"content-type": "application/json; charset=utf-8"})
    assert AppMagicCollector._looks_blocked(blocked) is True
    assert AppMagicCollector._looks_blocked(html_200) is True  # 挑战页是 HTML
    assert AppMagicCollector._looks_blocked(ok) is False
