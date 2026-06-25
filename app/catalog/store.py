import asyncio
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import ClassVar

# 版本目录 v2 §C 的 SQLite 单库四表：
#   versions          —— 某包当前已知版本（随刷新增长；downloadable 字段区分对外可下/内部 known-only）
#   version_sources   —— provenance + 各源「稳定」下载键
#   ledger            —— name↔code 账本，不可变、append-only、永不过期
#   collection_state  —— 驱动「全量 vs 增量」刷新 + 跨 worker 收集租约
# 阶段 10 只写 ledger（下载回填）；其余三表先建好，供阶段 11–14 使用。
SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    package TEXT NOT NULL,
    version_name TEXT NOT NULL,
    version_code INTEGER,
    first_seen_date TEXT,
    last_seen_date TEXT,
    downloadable INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (package, version_name)
);

CREATE TABLE IF NOT EXISTS version_sources (
    package TEXT NOT NULL,
    version_name TEXT NOT NULL,
    source TEXT NOT NULL,
    download_key TEXT,
    seen_at TEXT,
    PRIMARY KEY (package, version_name, source)
);

CREATE TABLE IF NOT EXISTS ledger (
    package TEXT NOT NULL,
    version_name TEXT NOT NULL,
    version_code INTEGER NOT NULL,
    source TEXT,
    first_seen_at TEXT,
    PRIMARY KEY (package, version_name, version_code)
);

CREATE TABLE IF NOT EXISTS collection_state (
    package TEXT PRIMARY KEY,
    last_full_at TEXT,
    last_refresh_at TEXT,
    source_cursors TEXT,
    collecting_owner TEXT,   -- 跨 worker 收集租约（§H）：当前持有者、起始时间、过期时间
    collecting_since TEXT,
    lease_expires TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- 单行：定时刷新的 leader 选主（§G）
    owner TEXT,
    since TEXT,
    expires TEXT
);
"""

# 阶段 10 建的旧库 collection_state 没有租约列；幂等补列，不丢数据。
_COLLECTION_STATE_LEASE_COLUMNS = ("collecting_owner", "collecting_since", "lease_expires")


class CatalogStore:
    """SQLite 连接管理 + 幂等建表 + WAL + per-package 写锁。

    - 连接按操作短开短关（本地 SQLite 极廉价），避免跨线程复用同一 connection 的限制。
    - WAL 在库文件上是持久属性，建表时设一次即可。
    - 进程内并发写用 per-package `asyncio.Lock`；跨进程/worker 写由调用方用 `BEGIN IMMEDIATE` 兜底
      （见 ledger）。锁字典是 ClassVar，因为 PackageDownloader 每请求新建一个 store 实例，
      只有共享锁才能真正串行化同包写。
    """

    _write_locks: ClassVar[dict[tuple[str, str], asyncio.Lock]] = {}
    _initialized: ClassVar[set[str]] = set()

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def connect(self) -> sqlite3.Connection:
        # isolation_level=None → 自动提交模式；事务由调用方显式 BEGIN IMMEDIATE / COMMIT 管控。
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self) -> None:
        key = str(self.db_path)
        if key in self._initialized:
            return
        # 多 worker 冷启动会同时初始化同一个新库。`PRAGMA journal_mode=WAL` 的模式切换需要短暂独占锁，
        # 且**不走 busy_timeout**（SQLite 直接返回 SQLITE_BUSY），并发时一个 worker 会拿到 "database is locked"。
        # 这里对建库重试若干次：等先到的 worker 建好 WAL+表后，其余 worker 立刻成功。
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(15):
            try:
                with closing(self.connect()) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(SCHEMA)
                    self._migrate(conn)
                self._initialized.add(key)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(0.1 * (attempt + 1))
        if last_error is not None:
            raise last_error

    def _migrate(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(collection_state)")}
        for column in _COLLECTION_STATE_LEASE_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE collection_state ADD COLUMN {column} TEXT")

    def write_lock(self, package: str) -> asyncio.Lock:
        key = (str(self.db_path), package)
        lock = self._write_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[key] = lock
        return lock
