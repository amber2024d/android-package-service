import sqlite3
from contextlib import closing

from app.auth.store import AuthStore


def _tables(db_path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_creates_four_tables_and_wal(tmp_path):
    db = tmp_path / "auth.sqlite"
    AuthStore(db)
    assert {"admin_user", "api_keys", "sessions", "oauth_states"} <= _tables(db)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_key_hash_unique_constraint(tmp_path):
    db = tmp_path / "auth.sqlite"
    store = AuthStore(db)
    with closing(store.connect()) as conn:
        conn.execute(
            "INSERT INTO api_keys (id, name, key_prefix, key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            ("1", "a", "aps_x", "hash", "t"),
        )
        try:
            conn.execute(
                "INSERT INTO api_keys (id, name, key_prefix, key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                ("2", "b", "aps_y", "hash", "t"),
            )
            raise AssertionError("duplicate key_hash should violate UNIQUE")
        except sqlite3.IntegrityError:
            pass


def test_schema_idempotent(tmp_path):
    db = tmp_path / "auth.sqlite"
    AuthStore(db)
    # 清缓存强制重跑建表脚本，验证 CREATE ... IF NOT EXISTS 可重入、不丢数据。
    AuthStore._initialized.discard(str(db))
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO admin_user (open_id, name, email, created_at) VALUES (?, ?, ?, ?)",
            ("ou_x", "n", "e", "t"),
        )
        conn.commit()
    AuthStore(db)  # 再次初始化不应报错、不应清空既有行
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_user").fetchone()[0] == 1
