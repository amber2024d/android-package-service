import asyncio
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Iterable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection
from typing import ClassVar

from app.catalog.collectors.base import Collector, VersionRecord
from app.catalog.ledger import version_sort_key
from app.catalog.store import CatalogStore

logger = logging.getLogger(__name__)

# versions：按 (package, version_name) 归并；code/日期取 COALESCE（已有不被 NULL 覆盖）；
# downloadable 取并集（MAX）——任一可下载源命中即 1，known-only 源（AppMagic）只贡献 0、不下调已有的 1。
# release_date（发布时间）只由 AppMagic 写入（非权威源传 NULL，COALESCE 不动），AppMagic 复采则覆盖为最新。
_VERSIONS_UPSERT = """
INSERT INTO versions (package, version_name, version_code, release_date, first_seen_date, last_seen_date, downloadable)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(package, version_name) DO UPDATE SET
  version_code = COALESCE(excluded.version_code, versions.version_code),
  release_date = COALESCE(excluded.release_date, versions.release_date),
  first_seen_date = COALESCE(versions.first_seen_date, excluded.first_seen_date),
  last_seen_date = COALESCE(excluded.last_seen_date, versions.last_seen_date),
  downloadable = MAX(versions.downloadable, excluded.downloadable)
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
        on_new_versions: Callable[[str, list[tuple[str, int | None]]], Awaitable[None]] | None = None,
    ):
        self.store = store
        self.collectors = list(collectors)
        self.ttl = timedelta(hours=ttl_hours)
        self.lease = timedelta(seconds=lease_seconds)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        # 主动归档钩子（阶段 16）：增量轮发现本轮新出现的 downloadable 版本时回调（首次全量不触发，避免回溯全窗）。
        self._on_new_versions = on_new_versions
        # 跨 worker 租约持有者标识：主机+pid 区分进程；id(self) 让同进程内不同实例（测试模拟双 worker）也可区分。
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{id(self)}"

    async def ensure_collected(self, package: str, *, need_history: bool = True, force: bool = False) -> None:
        """确保 package 已被收集。TTL 内或已全量且无需历史时直接返回（读路径只读库）。

        `force=True`：跳过 TTL/need_history 门，强制收集——给阶段 14 定时刷新用（定时任务是主刷新源、
        不受按需 TTL 限制，§G）。仍走收集单飞 + 跨 worker 租约。
        同包并发只跑一个收集任务：先到者建任务，后到者 await 同一个（不另起）。
        """
        if not self._needs_collection(package, need_history, force):
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

    def list_downloadable(self, package: str) -> list[tuple[str, int | None, str | None]]:
        """对外 `/versions` 的数据源：只出 `downloadable=1` 的 `(versionName, versionCode, releaseDate)`，按版本号降序（最新在前）。

        `releaseDate` 是 AppMagic 权威发布时间（无 AppMagic 覆盖则 None）。known-only（`downloadable=0`）不出
        （§B 决策①）。纯读库，不触发收集/刷新。
        """
        with closing(self.store.connect()) as conn:
            rows = conn.execute(
                "SELECT version_name, version_code, release_date FROM versions WHERE package = ? AND downloadable = 1",
                (package,),
            ).fetchall()
        rows.sort(key=lambda row: version_sort_key(row[0]), reverse=True)
        return [(name, code, release_date) for name, code, release_date in rows]

    def list_known_only(self, package: str) -> list[tuple[str, str | None]]:
        """缺口对账（内部，§17 步骤 4）：`downloadable=0` 的 known-only 版本 `(name, releaseDate)`，按版本号降序。

        「知道发布过、但当前无源可下」的清单（发布时间以 AppMagic 为准）——供监控告警 / 主动归档优先级，**不对外**。
        """
        with closing(self.store.connect()) as conn:
            rows = conn.execute(
                "SELECT version_name, release_date FROM versions WHERE package = ? AND downloadable = 0",
                (package,),
            ).fetchall()
        rows.sort(key=lambda row: version_sort_key(row[0]), reverse=True)
        return [(name, release_date) for name, release_date in rows]

    # ---- 决策：要不要收集 ----------------------------------------------------- #

    def _needs_collection(self, package: str, need_history: bool, force: bool = False) -> bool:
        with closing(self.store.connect()) as conn:
            row = conn.execute(
                "SELECT last_full_at, last_refresh_at FROM collection_state WHERE package = ?",
                (package,),
            ).fetchone()
        if row is None or row[0] is None:
            return True  # 从没全量采过
        if force:
            return True  # 定时刷新：强制增量，不受 TTL/need_history 门
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
            new_versions: list[tuple[str, int | None]] = []
            if gathered:
                new_versions = await asyncio.to_thread(self._persist, package, gathered, full)
            logger.info(
                "catalog collected %s (%s): sources=%s new=%d",
                package,
                "full" if full else "incremental",
                ",".join(sorted(gathered)),
                len(new_versions),
            )
            # 主动归档（阶段 16）：只在**增量**轮把本轮新出现的版本交给钩子；首次全量是建基线，不回溯整窗。
            if new_versions and not full and self._on_new_versions is not None:
                try:
                    await self._on_new_versions(package, new_versions)
                except Exception as exc:  # noqa: BLE001 — 归档钩子失败不影响收集
                    logger.warning("on_new_versions hook failed for %s: %s", package, exc)
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

    def _persist(self, package: str, gathered: dict[str, list[VersionRecord]], full: bool) -> list[tuple[str, int | None]]:
        """落库（upsert versions/version_sources + 刷 state）。返回本轮**新出现**的 downloadable 版本 (name, code)。"""
        now_iso = self._now().isoformat()
        downloadable_flags = {collector.source: collector.downloadable for collector in self.collectors}
        release_date_sources = {collector.source for collector in self.collectors if collector.provides_release_date}
        with closing(self.store.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = {
                    row[0] for row in conn.execute("SELECT version_name FROM versions WHERE package = ?", (package,))
                }
                cursors: dict[str, int] = {}
                collected: dict[str, int | None] = {}
                downloadable_names: set[str] = set()
                for source, records in gathered.items():
                    source_downloadable = 1 if downloadable_flags.get(source, True) else 0
                    # 发布时间只认权威源（AppMagic）；其它源传 NULL，不污染 release_date。
                    authoritative_date = source in release_date_sources
                    for record in records:
                        conn.execute(
                            _VERSIONS_UPSERT,
                            (
                                package,
                                record.version_name,
                                record.version_code,
                                record.release_date if authoritative_date else None,
                                record.release_date,
                                record.last_release_date or record.release_date,
                                source_downloadable,
                            ),
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
                        if source_downloadable:
                            downloadable_names.add(record.version_name)
                        # 同名多源：保留带 code 的那个，供归档/补全用
                        if record.version_name not in collected or (
                            collected[record.version_name] is None and record.version_code is not None
                        ):
                            collected[record.version_name] = record.version_code
                self._update_state(conn, package, full, cursors, now_iso)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        # 归档事件只含本轮新出现且**可下载**的版本——known-only（AppMagic）无源可下，不归档（§17）。
        return [
            (name, code)
            for name, code in collected.items()
            if name not in existing and name in downloadable_names
        ]

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
