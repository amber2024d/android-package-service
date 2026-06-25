import asyncio
import logging
import re
from contextlib import closing
from datetime import UTC, datetime

from app.catalog.store import CatalogStore

logger = logging.getLogger(__name__)

_VERSION_PART = re.compile(r"\d+")


def version_sort_key(version_name: str) -> tuple[int, ...]:
    """把 versionName 拆成数字段元组，用于「大体单调」比较。

    `"3.26.0" -> (3, 26, 0)`、`"2.41.1" -> (2, 41, 1)`。非数字部分忽略；
    取不到数字时回退 `(0,)`，保证可比较、不抛异常（账本里什么名字都可能进来）。
    """
    parts = tuple(int(p) for p in _VERSION_PART.findall(version_name))
    return parts or (0,)


class VersionLedger:
    """name↔code 账本：append-only、幂等、永不过期。

    每次成功下载解析产物 manifest 拿到权威 `(versionName, versionCode)` 后回填一行。
    主键 `(package, version_name, version_code)`，重复写用 `INSERT OR IGNORE` 幂等；
    同名多 code（一对多，§3.2）各自独立成行。
    """

    def __init__(self, store: CatalogStore):
        self.store = store

    async def upsert(self, package: str, version_name: str, version_code: int, source: str | None = None) -> bool:
        """回填一条 name↔code 事实。返回是否真正写入（已存在则 False）。

        缺字段（无包名/无版本名/无 code）直接跳过——账本只记完整事实。
        进程内同包写经 per-package 锁串行；DB 层用 `BEGIN IMMEDIATE` 兜底跨进程。
        """
        if not package or not version_name or version_code is None:
            return False
        async with self.store.write_lock(package):
            return await asyncio.to_thread(self._upsert_sync, package, version_name, int(version_code), source)

    def _upsert_sync(self, package: str, version_name: str, version_code: int, source: str | None) -> bool:
        with closing(self.store.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT version_name, version_code FROM ledger WHERE package = ?",
                    (package,),
                ).fetchall()
                self._warn_if_non_monotonic(package, version_name, version_code, existing)
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO ledger "
                    "(package, version_name, version_code, source, first_seen_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (package, version_name, version_code, source, datetime.now(UTC).isoformat()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return cursor.rowcount > 0

    def _warn_if_non_monotonic(
        self,
        package: str,
        version_name: str,
        version_code: int,
        existing: list[tuple[str, int]],
    ) -> None:
        """versionName 升序时 code 应大体单调。发现反序对（如 APKPure 把 2.41.1 错标 89）记 warning，不阻断。

        只检测「名更低却 code 更高」/「名更高却 code 更低」的反序；同名（一对多）跳过。
        命中即记一条 warning 就返回——目的是可观测，不是全量审计。
        """
        new_key = version_sort_key(version_name)
        for other_name, other_code in existing:
            if other_name == version_name:
                continue
            other_key = version_sort_key(other_name)
            inverted = (other_key < new_key and other_code > version_code) or (
                other_key > new_key and other_code < version_code
            )
            if inverted:
                logger.warning(
                    "ledger non-monotonic version_code for %s: %s=%d vs %s=%d",
                    package,
                    version_name,
                    version_code,
                    other_name,
                    other_code,
                )
                return
