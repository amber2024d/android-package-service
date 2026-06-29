import asyncio
import logging
import os
import socket
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta

from app.catalog.catalog import VersionCatalog
from app.catalog.runtime import build_catalog
from app.core.config import get_settings
from app.core.logging import configure_logging, log_event

logger = logging.getLogger(__name__)


class CatalogRefreshScheduler:
    """后台定时刷新（§G）：每 interval 对**已跟踪包**跑强制增量，把「保持新鲜」挪到后台。

    - **leader 选主**：`scheduler_lock` 单行 + `BEGIN IMMEDIATE`，多实例只一个 runner；租约带超时，
      leader 崩溃后超时可被他人重抢。
    - **限流与隔离**：逐包串行（避免同时多包过 Cloudflare / 触发封号），单包失败只记日志不阻断整轮；
      整轮 ok/failed 可观测。
    - 定时任务是**主刷新源**（`force=True` 旁路 TTL）；与下载触发的收集共用同一把收集单飞（§H）。
    """

    def __init__(
        self,
        catalog: VersionCatalog,
        *,
        interval_hours: float = 5.0,
        lease_seconds: int = 900,
        now_fn: Callable[[], datetime] | None = None,
        owner: str | None = None,
    ):
        self.catalog = catalog
        self.store = catalog.store
        self.interval_seconds = interval_hours * 3600.0
        self.lease = timedelta(seconds=lease_seconds)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._owner = owner or f"{socket.gethostname()}:{os.getpid()}"

    async def run_forever(self, stop: asyncio.Event) -> None:
        """周期循环：先等待一个 interval，之后到点刷新；`stop` 置位即退出。"""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
                break
            except asyncio.TimeoutError:
                pass  # interval 到点，跑一轮
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 — 整轮异常不该让循环挂掉
                log_event(logger, "catalog_refresh_round_failed", message=str(exc))

    async def run_once(self) -> dict:
        """跑一轮刷新。非 leader 直接跳过；leader 则逐包强制增量。返回可观测统计。"""
        if not await asyncio.to_thread(self._acquire_leadership):
            log_event(logger, "catalog_refresh_skipped", reason="not_leader")
            return {"leader": False, "packages": 0, "ok": 0, "failed": 0}

        packages = await asyncio.to_thread(self._tracked_packages)
        ok = 0
        failed = 0
        for package in packages:
            try:
                await self.catalog.ensure_collected(package, need_history=True, force=True)
                ok += 1
            except Exception as exc:  # noqa: BLE001 — 单包失败不阻断整轮
                failed += 1
                log_event(logger, "catalog_refresh_package_failed", package_name=package, message=str(exc))
        log_event(logger, "catalog_refresh_round_ok", packages=len(packages), ok=ok, failed=failed)
        return {"leader": True, "packages": len(packages), "ok": ok, "failed": failed}

    def _tracked_packages(self) -> list[str]:
        # 只刷已全量采过的包（= 服务真正用过的）；只有租约行、还没全量的不算已跟踪。
        with closing(self.store.connect()) as conn:
            rows = conn.execute(
                "SELECT package FROM collection_state WHERE last_full_at IS NOT NULL ORDER BY package"
            ).fetchall()
        return [row[0] for row in rows]

    def _acquire_leadership(self) -> bool:
        now = self._now_fn()
        expires = (now + self.lease).isoformat()
        with closing(self.store.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT owner, expires FROM scheduler_lock WHERE id = 1").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO scheduler_lock (id, owner, since, expires) VALUES (1, ?, ?, ?)",
                        (self._owner, now.isoformat(), expires),
                    )
                    acquired = True
                else:
                    owner, current_expires = row
                    expired = current_expires is not None and datetime.fromisoformat(current_expires) < now
                    acquired = owner is None or owner == self._owner or expired
                    if acquired:
                        conn.execute(
                            "UPDATE scheduler_lock SET owner = ?, since = ?, expires = ? WHERE id = 1",
                            (self._owner, now.isoformat(), expires),
                        )
                conn.execute("COMMIT")
                return acquired
            except Exception:
                conn.execute("ROLLBACK")
                raise


def main() -> None:
    configure_logging()
    settings = get_settings()
    if not settings.catalog_refresh_enabled:
        log_event(logger, "catalog_refresh_disabled")
        return
    settings.ensure_directories()
    scheduler = CatalogRefreshScheduler(
        build_catalog(settings),
        interval_hours=settings.catalog_refresh_interval_hours,
        lease_seconds=settings.catalog_scheduler_lease_seconds,
    )
    asyncio.run(scheduler.run_forever(asyncio.Event()))


if __name__ == "__main__":
    main()
