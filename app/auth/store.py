"""鉴权 SQLite 存储（阶段 18，§3.5）。

独立 `data/auth.sqlite`，与版本目录 `version-catalog.sqlite` 物理分库（安全敏感度/备份策略不同）。
连接短开短关 + WAL + 幂等建表，参照 `app/catalog/store.py` 的极简风格，但**不复用其 per-package 写锁**
（鉴权无按包并发语义；单管理员写用 `BEGIN IMMEDIATE` 兜底）。
"""

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import ClassVar

# §3.5 四表：admin_user（至多一行）/ api_keys（只存哈希）/ sessions（服务端会话）/ oauth_states（CSRF state + 回跳）。
SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_user (
    open_id TEXT PRIMARY KEY,
    name TEXT,
    email TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    next TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_oauth_states_expires ON oauth_states(expires_at);
"""


class AuthStore:
    """SQLite 连接管理 + 幂等建表 + WAL。"""

    _initialized: ClassVar[set[str]] = set()

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def connect(self) -> sqlite3.Connection:
        # isolation_level=None → 自动提交；事务由调用方显式 BEGIN IMMEDIATE / COMMIT 管控。
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self) -> None:
        key = str(self.db_path)
        if key in self._initialized:
            return
        # WAL 模式切换需短暂独占锁且不走 busy_timeout；多进程冷启动并发建库时重试若干次（同 CatalogStore）。
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(15):
            try:
                with closing(self.connect()) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(SCHEMA)
                self._initialized.add(key)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(0.1 * (attempt + 1))
        if last_error is not None:
            raise last_error
