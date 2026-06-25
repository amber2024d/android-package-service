import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

from app.catalog.catalog import VersionCatalog
from app.catalog.collectors.base import Collector, VersionRecord
from app.catalog.store import CatalogStore

START = datetime(2026, 1, 1, tzinfo=UTC)


class FakeCollector(Collector):
    def __init__(self, source: str, records: list[VersionRecord], *, fail: bool = False):
        self.source = source
        self._records = list(records)
        self.fail = fail
        self.calls = 0

    def set_records(self, records: list[VersionRecord]) -> None:
        self._records = list(records)

    async def collect(self, package: str) -> list[VersionRecord]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("source down")
        return list(self._records)


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def _catalog(tmp_path, collectors, *, clock=None, ttl_hours=6.0, lease_seconds=600):
    store = CatalogStore(tmp_path / "catalog.sqlite")
    cat = VersionCatalog(
        store,
        collectors,
        ttl_hours=ttl_hours,
        lease_seconds=lease_seconds,
        now_fn=clock or Clock(START),
    )
    return cat, store.db_path


def _versions(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(
            "SELECT version_name, version_code, first_seen_date, downloadable FROM versions ORDER BY version_name"
        ).fetchall()


def _sources(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(
            "SELECT version_name, source, download_key FROM version_sources ORDER BY version_name, source"
        ).fetchall()


def _state(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(
            "SELECT last_full_at, last_refresh_at, source_cursors, collecting_owner, lease_expires "
            "FROM collection_state"
        ).fetchone()


def test_full_collect_aggregates_and_marks_state(tmp_path):
    a = FakeCollector("apkpure", [VersionRecord("1.6.0", 288, download_key={"apkid": "x"}), VersionRecord("1.2.1", 116)])
    b = FakeCollector("aptoide", [VersionRecord("1.6.0", 288, download_key={"app_id": 1}), VersionRecord("1.0.0", None)])
    cat, db = _catalog(tmp_path, [a, b])

    asyncio.run(cat.ensure_collected("p"))

    assert [(r[0], r[1], r[3]) for r in _versions(db)] == [
        ("1.0.0", None, 1),
        ("1.2.1", 116, 1),
        ("1.6.0", 288, 1),
    ]
    # 1.6.0 由两源同时提供 -> 两条 provenance
    pairs = {(name, source) for name, source, _ in _sources(db)}
    assert ("1.6.0", "apkpure") in pairs and ("1.6.0", "aptoide") in pairs
    last_full, last_refresh, cursors, owner, lease = _state(db)
    assert last_full is not None and last_refresh is not None
    assert owner is None and lease is None  # 收集结束已释放租约
    assert a.calls == 1 and b.calls == 1


def test_within_ttl_skips_recollect(tmp_path):
    a = FakeCollector("apkpure", [VersionRecord("1.0.0", 1)])
    clock = Clock(START)
    cat, _ = _catalog(tmp_path, [a], clock=clock, ttl_hours=6)

    asyncio.run(cat.ensure_collected("p"))
    assert a.calls == 1
    clock.advance(hours=1)  # TTL 内
    asyncio.run(cat.ensure_collected("p"))
    assert a.calls == 1  # 直接读库，不重采


def test_incremental_adds_new_after_ttl_and_preserves_first_seen(tmp_path):
    a = FakeCollector("apkpure", [VersionRecord("1.0.0", 1, release_date="2026-01-01")])
    clock = Clock(START)
    cat, db = _catalog(tmp_path, [a], clock=clock, ttl_hours=6)

    asyncio.run(cat.ensure_collected("p"))
    clock.advance(hours=7)  # 超 TTL
    a.set_records([VersionRecord("1.0.0", 1, release_date="2099-12-31"), VersionRecord("1.1.0", 2)])
    asyncio.run(cat.ensure_collected("p"))

    assert a.calls == 2
    rows = _versions(db)
    assert [r[0] for r in rows] == ["1.0.0", "1.1.0"]
    first_seen = {r[0]: r[2] for r in rows}
    assert first_seen["1.0.0"] == "2026-01-01"  # 既有 first_seen 不被新值覆盖
    assert first_seen["1.1.0"] is None


def test_single_source_failure_isolated(tmp_path):
    a = FakeCollector("apkpure", [VersionRecord("1.0.0", 1)], fail=True)
    b = FakeCollector("aptoide", [VersionRecord("2.0.0", 2)])
    cat, db = _catalog(tmp_path, [a, b])

    asyncio.run(cat.ensure_collected("p"))

    assert [r[0] for r in _versions(db)] == ["2.0.0"]  # 只剩可用源的数据
    assert {source for _, source, _ in _sources(db)} == {"aptoide"}
    assert _state(db)[0] is not None  # 仍标记为已全量（整轮完成）
    assert a.calls == 1 and b.calls == 1


def test_single_flight_runs_one_task_per_package(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite")
    calls = {"n": 0}

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class GateCollector(Collector):
            source = "apkpure"

            async def collect(self, package):
                calls["n"] += 1
                started.set()
                await release.wait()
                return [VersionRecord("1.0.0", 1)]

        cat = VersionCatalog(store, [GateCollector()], now_fn=lambda: START)
        t1 = asyncio.create_task(cat.ensure_collected("p"))
        await started.wait()  # t1 已进入采集
        t2 = asyncio.create_task(cat.ensure_collected("p"))
        await asyncio.sleep(0)  # 让 t2 抵达 inflight await
        release.set()
        await asyncio.gather(t1, t2)

    asyncio.run(scenario())
    assert calls["n"] == 1
    assert [r[0] for r in _versions(store.db_path)] == ["1.0.0"]


def test_cross_worker_lease_blocks_then_reclaims_after_expiry(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite")
    a = FakeCollector("apkpure", [VersionRecord("1.0.0", 1)])
    b = FakeCollector("aptoide", [VersionRecord("2.0.0", 2)])
    clock = Clock(START)
    worker_a = VersionCatalog(store, [a], lease_seconds=600, now_fn=clock)
    worker_b = VersionCatalog(store, [b], lease_seconds=600, now_fn=clock)

    # worker A 抢到租约后「卡住」（模拟在跑/崩溃，未释放）
    assert worker_a._acquire_lease("p") is True

    # worker B 此时收集 -> 看到租约被占 -> 跳过
    asyncio.run(worker_b.ensure_collected("p"))
    assert b.calls == 0
    assert _versions(store.db_path) == []

    # 租约超时后，worker B 重抢并完成收集
    clock.advance(seconds=601)
    asyncio.run(worker_b.ensure_collected("p"))
    assert b.calls == 1
    assert [r[0] for r in _versions(store.db_path)] == ["2.0.0"]
