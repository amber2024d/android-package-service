import sqlite3
from contextlib import closing

from app.catalog.store import CatalogStore

EXPECTED_TABLES = {"versions", "version_sources", "ledger", "collection_state"}


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
