import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

from app.catalog import scheduler as scheduler_module
from app.catalog.catalog import VersionCatalog
from app.catalog.collectors.base import Collector, VersionRecord
from app.catalog.scheduler import CatalogRefreshScheduler
from app.catalog.store import CatalogStore
from app.core.config import get_settings

START = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)


class FakeCatalog:
    """鸭子类型 catalog：持有真实 store（供 leader/已跟踪查询），记录/可控 ensure_collected。"""

    def __init__(self, store, *, fail_for=()):
        self.store = store
        self.fail_for = set(fail_for)
        self.collected: list[str] = []

    async def ensure_collected(self, package, *, need_history=True, force=False):
        self.collected.append(package)
        if package in self.fail_for:
            raise RuntimeError("collect blew up")


class FakeCollector(Collector):
    def __init__(self, source, records):
        self.source = source
        self._records = records
        self.calls = 0

    async def collect(self, package):
        self.calls += 1
        return list(self._records)


def _store(tmp_path, *, tracked=(), untracked=()):
    store = CatalogStore(tmp_path / "catalog.sqlite")
    with closing(sqlite3.connect(store.db_path)) as conn:
        for package in tracked:
            conn.execute(
                "INSERT INTO collection_state(package, last_full_at, last_refresh_at) VALUES(?,?,?)",
                (package, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        for package in untracked:  # 只有租约行、还没全量 -> 不算已跟踪
            conn.execute("INSERT INTO collection_state(package, collecting_owner) VALUES(?, 'x')", (package,))
        conn.commit()
    return store


def test_run_once_refreshes_only_tracked_packages(tmp_path):
    store = _store(tmp_path, tracked=["com.a", "com.b"], untracked=["com.c"])
    catalog = FakeCatalog(store)
    scheduler = CatalogRefreshScheduler(catalog, now_fn=Clock(START))

    stats = asyncio.run(scheduler.run_once())

    assert stats == {"leader": True, "packages": 2, "ok": 2, "failed": 0}
    assert sorted(catalog.collected) == ["com.a", "com.b"]  # 未跟踪的 com.c 不刷


def test_single_package_failure_does_not_abort_round(tmp_path):
    store = _store(tmp_path, tracked=["com.a", "com.bad", "com.c"])
    catalog = FakeCatalog(store, fail_for={"com.bad"})
    scheduler = CatalogRefreshScheduler(catalog, now_fn=Clock(START))

    stats = asyncio.run(scheduler.run_once())

    assert stats["ok"] == 2 and stats["failed"] == 1
    assert sorted(catalog.collected) == ["com.a", "com.bad", "com.c"]  # 失败后仍继续其余


def test_non_leader_skips(tmp_path):
    store = _store(tmp_path, tracked=["com.a"])
    # 另一个 worker 已持租约（未过期）
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute(
            "INSERT INTO scheduler_lock(id, owner, since, expires) VALUES(1, 'other', ?, ?)",
            (START.isoformat(), (START + timedelta(seconds=900)).isoformat()),
        )
        conn.commit()
    catalog = FakeCatalog(store)
    scheduler = CatalogRefreshScheduler(catalog, now_fn=Clock(START))

    stats = asyncio.run(scheduler.run_once())

    assert stats["leader"] is False
    assert catalog.collected == []  # 非 leader 不刷


def test_leader_reclaimed_after_lease_expiry(tmp_path):
    store = _store(tmp_path, tracked=["com.a"])
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute(
            "INSERT INTO scheduler_lock(id, owner, since, expires) VALUES(1, 'dead-worker', ?, ?)",
            (START.isoformat(), (START + timedelta(seconds=900)).isoformat()),
        )
        conn.commit()
    catalog = FakeCatalog(store)
    clock = Clock(START)
    scheduler = CatalogRefreshScheduler(catalog, lease_seconds=900, now_fn=clock)

    assert asyncio.run(scheduler.run_once())["leader"] is False  # 租约未过期 -> 跳过
    clock.advance(seconds=901)  # 原 leader 崩溃后租约超时
    assert asyncio.run(scheduler.run_once())["leader"] is True  # 可重抢
    assert catalog.collected == ["com.a"]


def test_force_refresh_bypasses_ttl(tmp_path):
    # 真实 VersionCatalog：刚全量采过（TTL 内），定时刷新 force=True 仍应再跑增量
    store = _store(tmp_path, tracked=["com.a"])
    collector = FakeCollector("apkpure", [VersionRecord("1.0.0", 1)])
    catalog = VersionCatalog(store, [collector], ttl_hours=6, now_fn=Clock(START))
    scheduler = CatalogRefreshScheduler(catalog, now_fn=Clock(START))

    # 不带 force：TTL 内不重采
    asyncio.run(catalog.ensure_collected("com.a", need_history=True))
    assert collector.calls == 0  # last_refresh 就是 START，TTL 内 -> 跳过

    # 定时刷新一轮：force 旁路 TTL -> 重采
    asyncio.run(scheduler.run_once())
    assert collector.calls == 1


def test_run_forever_does_not_refresh_before_first_interval(tmp_path):
    store = _store(tmp_path, tracked=["com.a"])
    scheduler = CatalogRefreshScheduler(FakeCatalog(store), interval_hours=999, now_fn=Clock(START))
    calls = {"n": 0}

    async def scenario():
        stop = asyncio.Event()

        async def counting():
            calls["n"] += 1
            return {"leader": True, "packages": 0, "ok": 0, "failed": 0}

        scheduler.run_once = counting
        stop.set()
        await asyncio.wait_for(scheduler.run_forever(stop), timeout=2)

    asyncio.run(scenario())
    assert calls["n"] == 0  # 重启/启动后不会立即刷新


def test_run_forever_refreshes_after_interval(tmp_path):
    store = _store(tmp_path, tracked=["com.a"])
    scheduler = CatalogRefreshScheduler(FakeCatalog(store), interval_hours=0.000001, now_fn=Clock(START))
    calls = {"n": 0}

    async def scenario():
        stop = asyncio.Event()

        async def counting():
            calls["n"] += 1
            stop.set()
            return {"leader": True, "packages": 0, "ok": 0, "failed": 0}

        scheduler.run_once = counting
        await asyncio.wait_for(scheduler.run_forever(stop), timeout=2)

    asyncio.run(scenario())
    assert calls["n"] == 1


def test_scheduler_main_returns_when_refresh_disabled(tmp_path, caplog):
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.catalog_refresh_enabled = False
    try:
        with caplog.at_level(logging.INFO):
            scheduler_module.main()
    finally:
        get_settings.cache_clear()

    assert "catalog_refresh_disabled" in caplog.text
