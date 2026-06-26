import asyncio

from app.catalog.collectors.appmagic import AppMagicCollector
from app.catalog.session.appmagic_session import AppMagicSession


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


def _collector(session: AppMagicSession) -> AppMagicCollector:
    collector = AppMagicCollector(session)

    async def fake_request_json(package: str) -> dict:
        return _payload()

    collector._request_json = fake_request_json
    return collector


def test_is_known_only_source():
    collector = AppMagicCollector(AppMagicSession("cf", "tok"))
    assert collector.downloadable is False
    assert collector.source == "appmagic"
    assert collector.provides_release_date is True  # 发布时间以 AppMagic 为准


def test_records_dedup_by_name_with_first_last_dates():
    records = asyncio.run(_collector(AppMagicSession("cf", "tok")).collect("com.pkg"))
    by_name = {r.version_name: r for r in records}

    assert set(by_name) == {"1.6.0", "1.5.0", "1.4.0"}
    assert by_name["1.6.0"].version_code is None  # known-only：无 code
    assert by_name["1.6.0"].release_date == "2024-03-22"  # 首次
    assert by_name["1.6.0"].last_release_date == "2024-03-25"  # 末次
    assert by_name["1.5.0"].release_date == "2024-01-10"
    assert by_name["1.4.0"].release_date is None  # 无日期容忍
    assert by_name["1.6.0"].download_key == {"release_date": "2024-03-22"}


def test_unavailable_session_degrades_to_empty():
    session = AppMagicSession(None, None)  # 缺 cookie → 不可用
    collector = AppMagicCollector(session)
    called = {"n": 0}

    async def boom(package):
        called["n"] += 1
        return {}

    collector._request_json = boom
    assert asyncio.run(collector.collect("com.pkg")) == []
    assert called["n"] == 0  # 不可用时根本不发请求


def test_invalidate_marks_session_unavailable():
    session = AppMagicSession("cf", "tok")
    assert session.available() is True
    session.invalidate()
    assert session.available() is False
