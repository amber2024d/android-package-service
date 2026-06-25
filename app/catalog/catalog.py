import asyncio
import json
import logging
import os
import socket
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection
from typing import ClassVar

from app.catalog.collectors.base import Collector, VersionRecord
from app.catalog.ledger import version_sort_key
from app.catalog.store import CatalogStore

logger = logging.getLogger(__name__)

# versions：按 (package, version_name) 归并；code/日期取 COALESCE（已有不被 NULL 覆盖），downloadable 取并集（这里都=1）。
_VERSIONS_UPSERT = """
INSERT INTO versions (package, version_name, version_code, first_seen_date, last_seen_date, downloadable)
VALUES (?, ?, ?, ?, ?, 1)
ON CONFLICT(package, version_name) DO UPDATE SET
  version_code = COALESCE(excluded.version_code, versions.version_code),
  first_seen_date = COALESCE(versions.first_seen_date, excluded.first_seen_date),
  last_seen_date = COALESCE(excluded.last_seen_date, versions.last_seen_date),
  downloadable = 1
"""

# version_sources：provenance + 各源稳定下载键，按 (package, version_name, source) 覆盖更新。
_SOURCES_UPSERT = """
INSERT INTO version_sources (package, version_name, source, download_key, seen_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(package, version_name, source) DO UPDATE SET
  download_key = excluded.download_key,
  seen_at = excluded.seen_at
"""


class VersionCatalog:
    """唯一的「版本枚举」层：聚合多源到 SQLite，按需收集（全量/增量），收集单飞去重。

    本阶段（11）只灌数据：`ensure_collected` 被 `/versions` 首采（await）或下载路径（fire-and-forget，阶段 12）
    或定时刷新（阶段 14）调用，同一包始终只跑一个收集任务（进程内 task 去重 + 跨 worker SQLite 租约）。
    """

    # 进程内单飞：同一 (db, package) 只挂一个在跑的收集任务，后到者复用它。
    _inflight: ClassVar[dict[tuple[str, str], asyncio.Task]] = {}

    def __init__(
        self,
        store: CatalogStore,
        collectors: Iterable[Collector],
        *,
        ttl_hours: float = 6.0,
        lease_seconds: int = 600,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.collectors = list(collectors)
        self.ttl = timedelta(hours=ttl_hours)
        self.lease = timedelta(seconds=lease_seconds)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        # 跨 worker 租约持有者标识：主机+pid 区分进程；id(self) 让同进程内不同实例（测试模拟双 worker）也可区分。
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{id(self)}"

    async def ensure_collected(self, package: str, *, need_history: bool = True) -> None:
        """确保 package 已被收集。TTL 内或已全量且无需历史时直接返回（读路径只读库）。

        同包并发只跑一个收集任务：先到者建任务，后到者 await 同一个（不另起）。
        """
        if not self._needs_collection(package, need_history):
            return
        key = (str(self.store.db_path), package)
        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.ensure_future(self._collect_once(package))
            self._inflight[key] = task
        try:
            await task
        finally:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    def list_downloadable(self, package: str) -> list[tuple[str, int | None]]:
        """对外 `/versions` 的数据源：只出 `downloadable=1` 的 `(versionName, versionCode)`，按版本号降序（最新在前）。

        known-only（`downloadable=0`）不出（§B 决策①）。纯读库，不触发收集/刷新。
        """
        with closing(self.store.connect()) as conn:
            rows = conn.execute(
                "SELECT version_name, version_code FROM versions WHERE package = ? AND downloadable = 1",
                (package,),
            ).fetchall()
        rows.sort(key=lambda row: version_sort_key(row[0]), reverse=True)
        return [(name, code) for name, code in rows]

    # ---- 决策：要不要收集 ----------------------------------------------------- #

    def _needs_collection(self, package: str, need_history: bool) -> bool:
        with closing(self.store.connect()) as conn:
            row = conn.execute(
                "SELECT last_full_at, last_refresh_at FROM collection_state WHERE package = ?",
                (package,),
            ).fetchone()
        if row is None or row[0] is None:
            return True  # 从没全量采过
        if not need_history:
            return False
        last_refresh = row[1]
        if last_refresh is None:
            return True
        return self._now() - datetime.fromisoformat(last_refresh) > self.ttl

    # ---- 收集一次（持租约 → 抓取 → 落库 → 释放） ------------------------------- #

    async def _collect_once(self, package: str) -> None:
        if not await asyncio.to_thread(self._acquire_lease, package):
            logger.info("catalog collect skipped, lease held by another worker: %s", package)
            return
        try:
            full = await asyncio.to_thread(self._is_first_time, package)
            gathered = await self._gather(package, full)
            if gathered:
                await asyncio.to_thread(self._persist, package, gathered, full)
            logger.info(
                "catalog collected %s (%s): sources=%s",
                package,
                "full" if full else "incremental",
                ",".join(sorted(gathered)),
            )
        finally:
            await asyncio.to_thread(self._release_lease, package)

    async def _gather(self, package: str, full: bool) -> dict[str, list[VersionRecord]]:
        async def run(collector: Collector) -> tuple[str, list[VersionRecord] | None]:
            try:
                records = await (collector.collect(package) if full else collector.collect_recent(package))
                return collector.source, records
            except Exception as exc:  # noqa: BLE001 — 单源失败不阻断其余源（§F 失败隔离）
                logger.warning("catalog source %s failed for %s: %s", collector.source, package, exc)
                return collector.source, None

        results = await asyncio.gather(*(run(collector) for collector in self.collectors))
        return {source: records for source, records in results if records is not None}

    def _persist(self, package: str, gathered: dict[str, list[VersionRecord]], full: bool) -> None:
        now_iso = self._now().isoformat()
        with closing(self.store.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursors: dict[str, int] = {}
                for source, records in gathered.items():
                    for record in records:
                        conn.execute(
                            _VERSIONS_UPSERT,
                            (package, record.version_name, record.version_code, record.release_date, record.release_date),
                        )
                        conn.execute(
                            _SOURCES_UPSERT,
                            (
                                package,
                                record.version_name,
                                source,
                                json.dumps(record.download_key or {}, sort_keys=True, ensure_ascii=False),
                                now_iso,
                            ),
                        )
                        if record.version_code is not None:
                            cursors[source] = max(cursors.get(source, record.version_code), record.version_code)
                self._update_state(conn, package, full, cursors, now_iso)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _update_state(self, conn: Connection, package: str, full: bool, cursors: dict[str, int], now_iso: str) -> None:
        row = conn.execute(
            "SELECT source_cursors, last_full_at FROM collection_state WHERE package = ?",
            (package,),
        ).fetchone()
        merged = json.loads(row[0]) if row and row[0] else {}
        merged.update({source: code for source, code in cursors.items()})
        last_full = now_iso if full else (row[1] if row else None)
        conn.execute(
            "UPDATE collection_state SET last_full_at = ?, last_refresh_at = ?, source_cursors = ? WHERE package = ?",
            (last_full, now_iso, json.dumps(merged, sort_keys=True), package),
        )

    # ---- 跨 worker 收集租约（§H：BEGIN IMMEDIATE 抢、带超时防崩溃占用） --------- #

    def _acquire_lease(self, package: str) -> bool:
        now = self._now()
        expires = (now + self.lease).isoformat()
        with closing(self.store.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT collecting_owner, lease_expires FROM collection_state WHERE package = ?",
                    (package,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO collection_state (package, collecting_owner, collecting_since, lease_expires) "
                        "VALUES (?, ?, ?, ?)",
                        (package, self._owner, now.isoformat(), expires),
                    )
                    acquired = True
                else:
                    owner, lease_expires = row
                    expired = lease_expires is not None and datetime.fromisoformat(lease_expires) < now
                    acquired = owner is None or owner == self._owner or expired
                    if acquired:
                        conn.execute(
                            "UPDATE collection_state SET collecting_owner = ?, collecting_since = ?, lease_expires = ? "
                            "WHERE package = ?",
                            (self._owner, now.isoformat(), expires, package),
                        )
                conn.execute("COMMIT")
                return acquired
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _release_lease(self, package: str) -> None:
        with closing(self.store.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 只释放自己持有的租约：避免超时后被他人重抢、我们又把它的租约清掉。
                conn.execute(
                    "UPDATE collection_state SET collecting_owner = NULL, collecting_since = NULL, lease_expires = NULL "
                    "WHERE package = ? AND collecting_owner = ?",
                    (package, self._owner),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _is_first_time(self, package: str) -> bool:
        with closing(self.store.connect()) as conn:
            row = conn.execute(
                "SELECT last_full_at FROM collection_state WHERE package = ?",
                (package,),
            ).fetchone()
        return row is None or row[0] is None

    def _now(self) -> datetime:
        return self._now_fn()
