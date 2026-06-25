import asyncio

from app.catalog.collectors.aptoide import AptoideCollector


def _payload() -> dict:
    return {
        "info": {"status": "OK"},
        "nodes": {
            "meta": {
                "data": {
                    "id": 999,
                    "file": {"vername": "9.22.5.3", "vercode": 12060, "md5sum": "abc", "added": "2024-01-02 10:00:00"},
                }
            },
            "versions": {
                "list": [
                    {"id": 888, "file": {"vername": "9.22.5.2", "vercode": 12059, "md5sum": "def"}},
                    {"id": 777, "file": {"vername": "9.22.5.3", "vercode": 12060, "md5sum": "zzz"}},  # 同名 -> meta 胜，去重
                    {"id": 444, "file": {"vername": "9.0.0", "md5sum": "mmm"}},  # 缺 vercode -> code=None 仍收
                    {"id": 666, "file": {"vername": None, "vercode": 12000}},  # 缺 name -> 丢弃
                    {"id": 555, "file": {"vercode": 11000}},  # 缺 name -> 丢弃
                    "garbage",  # 非 dict -> 丢弃
                ]
            },
        },
    }


def _collector() -> AptoideCollector:
    collector = AptoideCollector()

    async def fake_request_json(path: str) -> dict:
        assert path.startswith("app/get/package_name=")
        return _payload()

    collector._request_json = fake_request_json
    return collector


def test_collect_parses_dedups_and_tolerates_dirty():
    records = asyncio.run(_collector().collect("cm.aptoide.pt"))
    by_name = {r.version_name: r for r in records}

    assert set(by_name) == {"9.22.5.3", "9.22.5.2", "9.0.0"}
    # meta（当前版）优先于历史列表里的同名项
    assert by_name["9.22.5.3"].version_code == 12060
    assert by_name["9.22.5.3"].download_key == {"app_id": 999, "md5": "abc"}
    assert by_name["9.22.5.3"].release_date == "2024-01-02"
    # 历史项
    assert by_name["9.22.5.2"].download_key == {"app_id": 888, "md5": "def"}
    assert by_name["9.22.5.2"].release_date is None
    # 缺 vercode 仍记录，code=None
    assert by_name["9.0.0"].version_code is None
    assert by_name["9.0.0"].download_key == {"app_id": 444, "md5": "mmm"}


def test_collect_handles_missing_nodes():
    collector = AptoideCollector()

    async def empty(path):
        return {"info": {"status": "OK"}}

    collector._request_json = empty
    assert asyncio.run(collector.collect("x")) == []
