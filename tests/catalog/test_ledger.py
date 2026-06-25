import asyncio
import logging
import sqlite3
from contextlib import closing

from app.catalog.ledger import VersionLedger, version_sort_key
from app.catalog.store import CatalogStore


def _ledger(tmp_path) -> tuple[VersionLedger, object]:
    store = CatalogStore(tmp_path / "catalog.sqlite")
    return VersionLedger(store), store.db_path


def _rows(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT package, version_name, version_code, source FROM ledger ORDER BY version_code"
        ).fetchall()


def test_version_sort_key():
    assert version_sort_key("3.26.0") == (3, 26, 0)
    assert version_sort_key("2.41.1") == (2, 41, 1)
    assert version_sort_key("nonsense") == (0,)


def test_upsert_records_fact(tmp_path):
    ledger, db = _ledger(tmp_path)
    written = asyncio.run(ledger.upsert("com.app", "1.2.0", 120, "apkpure-signed"))
    assert written is True
    assert _rows(db) == [("com.app", "1.2.0", 120, "apkpure-signed")]
    with closing(sqlite3.connect(db)) as conn:
        first_seen = conn.execute("SELECT first_seen_at FROM ledger").fetchone()[0]
    assert first_seen  # 写入了时间戳


def test_upsert_is_idempotent(tmp_path):
    ledger, db = _ledger(tmp_path)

    async def scenario():
        first = await ledger.upsert("com.app", "1.2.0", 120, "apkpure-signed")
        second = await ledger.upsert("com.app", "1.2.0", 120, "google-play")
        return first, second

    first, second = asyncio.run(scenario())
    assert first is True
    assert second is False  # 已存在，幂等不重复
    assert _rows(db) == [("com.app", "1.2.0", 120, "apkpure-signed")]


def test_same_name_multiple_codes_each_row(tmp_path):
    ledger, db = _ledger(tmp_path)

    async def scenario():
        await ledger.upsert("com.app", "1.2.0", 120, "google-play")
        await ledger.upsert("com.app", "1.2.0", 121, "google-play")

    asyncio.run(scenario())
    assert _rows(db) == [
        ("com.app", "1.2.0", 120, "google-play"),
        ("com.app", "1.2.0", 121, "google-play"),
    ]


def test_incomplete_facts_skipped(tmp_path):
    ledger, db = _ledger(tmp_path)

    async def scenario():
        return [
            await ledger.upsert("", "1.0.0", 1, "fake"),
            await ledger.upsert("com.app", "", 1, "fake"),
            await ledger.upsert("com.app", "1.0.0", None, "fake"),
        ]

    results = asyncio.run(scenario())
    assert results == [False, False, False]
    assert _rows(db) == []


def test_non_monotonic_logs_warning_but_still_inserts(tmp_path, caplog):
    ledger, db = _ledger(tmp_path)

    async def scenario():
        await ledger.upsert("com.app", "3.0.0", 1000, "apkpure-signed")
        with caplog.at_level(logging.WARNING):
            # 名更低（2.0.0 < 3.0.0）却 code 更高（2000 > 1000）—— 反序，应记 warning，但仍写入
            await ledger.upsert("com.app", "2.0.0", 2000, "apkpure-signed")

    asyncio.run(scenario())
    assert "non-monotonic" in caplog.text
    assert len(_rows(db)) == 2


def test_monotonic_sequence_no_warning(tmp_path, caplog):
    ledger, _ = _ledger(tmp_path)

    async def scenario():
        with caplog.at_level(logging.WARNING):
            await ledger.upsert("com.app", "2.0.0", 200, "apkpure-signed")
            await ledger.upsert("com.app", "3.0.0", 1400, "apkpure-signed")

    asyncio.run(scenario())
    assert "non-monotonic" not in caplog.text
