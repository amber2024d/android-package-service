import sqlite3
import threading
from contextlib import closing

from app.catalog.store import CatalogStore

EXPECTED_TABLES = {"versions", "version_sources", "ledger", "collection_state", "scheduler_lock"}


def _table_names(db_path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def test_schema_creates_four_tables(tmp_path):
    db = tmp_path / "catalog.sqlite"
    CatalogStore(db)
    assert EXPECTED_TABLES <= _table_names(db)


def test_schema_init_is_idempotent_and_keeps_data(tmp_path):
    db = tmp_path / "catalog.sqlite"
    store = CatalogStore(db)
    with closing(store.connect()) as conn:
        conn.execute(
            "INSERT INTO ledger(package, version_name, version_code, source, first_seen_at) "
            "VALUES('p', '1.0.0', 1, 'fake', '2026-01-01T00:00:00Z')"
        )

    # 清掉进程内「已建表」缓存，强制重跑建表迁移；CREATE IF NOT EXISTS 不应报错也不丢数据。
    CatalogStore._initialized.discard(str(db))
    CatalogStore(db)

    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT package, version_code FROM ledger").fetchall()
    assert rows == [("p", 1)]


def test_concurrent_fresh_db_init_does_not_lock(tmp_path):
    """回归：多 worker 冷启动同时初始化同一个新库不应崩。

    `PRAGMA journal_mode=WAL` 的模式切换需短暂独占锁且**不走 busy_timeout**（SQLite 直接返回
    SQLITE_BUSY），并发建库时一个 worker 会拿到 "database is locked"——正式镜像 --workers 2 首启崩溃的根因。
    `_ensure_schema` 须重试穿过；这里用 barrier 把多个连接对齐到同一瞬间抢 WAL 切换，断言无人报错、库建好。
    """
    db = tmp_path / "race.sqlite"
    CatalogStore._initialized.discard(str(db))  # 强制真跑建库（不吃进程内缓存）

    workers = 16
    barrier = threading.Barrier(workers)
    errors: list[Exception] = []

    def init() -> None:
        barrier.wait()  # 对齐到同一瞬间，最大化 WAL 切换竞争
        try:
            CatalogStore(db)
        except Exception as exc:  # noqa: BLE001 — 回归点：并发建库不该抛
            errors.append(exc)

    threads = [threading.Thread(target=init) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"并发建库报错: {errors}"
    assert EXPECTED_TABLES <= _table_names(db)


def test_ensure_schema_retries_through_locked(tmp_path, monkeypatch):
    """回归（跨平台确定性）：建库时遇 "database is locked" 必须重试穿过，而不是直接崩。

    上面的并发用例只在 Linux 上真触发 WAL 锁竞争（macOS 的 SQLite 对 WAL 切换会等 busy_timeout）。
    这里直接注入头两次 locked，断言 `_ensure_schema` 重试到成功——任何平台都能锁死「重试」这个修复点。
    """
    db = tmp_path / "retry.sqlite"
    CatalogStore._initialized.discard(str(db))

    real_connect = CatalogStore.connect
    attempts = {"n": 0}

    def flaky_connect(self):
        attempts["n"] += 1
        if attempts["n"] <= 2:  # 头两次模拟并发独占锁竞争
            raise sqlite3.OperationalError("database is locked")
        return real_connect(self)

    monkeypatch.setattr(CatalogStore, "connect", flaky_connect)

    CatalogStore(db)  # 无重试时第一次就抛 → 此行会失败

    assert attempts["n"] >= 3  # 确实重试穿过了前两次 locked
    assert EXPECTED_TABLES <= _table_names(db)


def test_wal_mode_enabled(tmp_path):
    db = tmp_path / "catalog.sqlite"
    store = CatalogStore(db)
    with closing(store.connect()) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_write_lock_is_stable_per_package(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite")
    assert store.write_lock("com.a") is store.write_lock("com.a")
    assert store.write_lock("com.a") is not store.write_lock("com.b")
