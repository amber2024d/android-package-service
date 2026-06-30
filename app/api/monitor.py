"""监控面板的数据聚合层 + 路由。

只读版本目录 SQLite（`download_jobs` / `versions` / `version_sources` / `ledger` / `collection_state`），
把任务状态、provider 流转、近 N 天耗时与成功率、收录规模聚合成一份快照给前端轮询。

- 纯读：不写任何表，不触发采集/下载，可安全高频轮询。
- 经 `CatalogStore` 访问：实例化即幂等建表，老库缺 `download_jobs` 也会补上（返回空任务而非报错）。
- 成功任务的「命中来源」从 `artifact_path` 的 `artifacts/{provider}/...` 段还原（job 行只记请求时的 preferred provider）；
  失败任务的「provider 流转链」直接读 `provider_errors`（按 auto fallback 的尝试顺序）。
- 时间戳统一是 `datetime.now(UTC).isoformat()`，UTC ISO-8601 可按字符串比较窗口下界。
"""

import json
import logging
import math
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.catalog.store import CatalogStore
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

# 耗时直方图分桶（下载耗时 = finished - started），边界单位秒；最后一桶为开区间。
_DURATION_BUCKETS = [
    (5, "0–5s"),
    (15, "5–15s"),
    (30, "15–30s"),
    (60, "30–60s"),
    (180, "1–3m"),
    (600, "3–10m"),
    (math.inf, ">10m"),
]


class MonitorService:
    """把版本目录库聚合成一份监控快照。所有方法纯读、无副作用。"""

    def __init__(
        self,
        store: CatalogStore,
        *,
        artifacts_dir: Path | None = None,
        now_fn=None,
        recent_limit: int = 25,
        active_limit: int = 200,
    ):
        self.store = store
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self.recent_limit = recent_limit
        self.active_limit = active_limit

    # ---- 对外快照 ----------------------------------------------------------- #

    def snapshot(self, *, days: int = 7) -> dict:
        now = self._now_fn()
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            overview = self._overview(conn, now)
            tasks = self._tasks(conn, now)
            window_jobs = self._window_jobs(conn, now, days)
        providers = self._providers(window_jobs)
        analytics = self._analytics(window_jobs, now, days)
        return {
            "generatedAt": now.isoformat(),
            "windowDays": days,
            "overview": overview,
            "tasks": tasks,
            "providers": providers,
            "analytics": analytics,
        }

    # ---- 概览：收录规模 + 任务计数 + 采集状态 -------------------------------- #

    def _overview(self, conn: sqlite3.Connection, now: datetime) -> dict:
        pkgs, total_versions, downloadable = conn.execute(
            "SELECT COUNT(DISTINCT package), COUNT(*), COALESCE(SUM(downloadable), 0) FROM versions"
        ).fetchone()
        sources = [
            {"source": row["source"], "versions": row["c"]}
            for row in conn.execute(
                "SELECT source, COUNT(*) AS c FROM version_sources GROUP BY source ORDER BY c DESC"
            )
        ]
        ledger_facts = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]

        job_counts = {QUEUED: 0, RUNNING: 0, SUCCEEDED: 0, FAILED: 0}
        for row in conn.execute("SELECT status, COUNT(*) AS c FROM download_jobs GROUP BY status"):
            job_counts[row["status"]] = row["c"]
        job_counts["total"] = sum(v for k, v in job_counts.items() if k != "total")

        tracked = conn.execute("SELECT COUNT(*) FROM collection_state").fetchone()[0]
        now_iso = now.isoformat()
        collecting = conn.execute(
            "SELECT COUNT(*) FROM collection_state "
            "WHERE collecting_owner IS NOT NULL AND (lease_expires IS NULL OR lease_expires > ?)",
            (now_iso,),
        ).fetchone()[0]
        last_refresh = conn.execute(
            "SELECT MAX(last_refresh_at) FROM collection_state"
        ).fetchone()[0]

        return {
            "packages": pkgs or 0,
            "versions": {
                "total": total_versions or 0,
                "downloadable": downloadable or 0,
                "knownOnly": (total_versions or 0) - (downloadable or 0),
            },
            "sources": sources,
            "ledgerFacts": ledger_facts or 0,
            "jobs": job_counts,
            "catalog": {
                "trackedPackages": tracked or 0,
                "collectingNow": collecting or 0,
                "lastRefreshAt": last_refresh,
            },
        }

    # ---- 任务看板：进行中 / 排队 / 最近失败 / 最近成功 ---------------------- #

    def _tasks(self, conn: sqlite3.Connection, now: datetime) -> dict:
        def rows(sql: str, params: tuple = ()) -> list[dict]:
            return [self._job_to_dict(row, now) for row in conn.execute(sql, params)]

        return {
            "running": rows(
                "SELECT * FROM download_jobs WHERE status = ? ORDER BY started_at LIMIT ?",
                (RUNNING, self.active_limit),
            ),
            "queued": rows(
                "SELECT * FROM download_jobs WHERE status = ? ORDER BY created_at LIMIT ?",
                (QUEUED, self.active_limit),
            ),
            "recentFailed": rows(
                "SELECT * FROM download_jobs WHERE status = ? ORDER BY finished_at DESC, updated_at DESC LIMIT ?",
                (FAILED, self.recent_limit),
            ),
            "recentSucceeded": rows(
                "SELECT * FROM download_jobs WHERE status = ? ORDER BY finished_at DESC, updated_at DESC LIMIT ?",
                (SUCCEEDED, self.recent_limit),
            ),
        }

    def _job_to_dict(self, row: sqlite3.Row, now: datetime) -> dict:
        created = _parse(row["created_at"])
        started = _parse(row["started_at"])
        finished = _parse(row["finished_at"])
        status = row["status"]

        if status == RUNNING:
            elapsed = _ms(started or created, now)
        elif status == QUEUED:
            elapsed = _ms(created, now)
        else:
            elapsed = _ms(created, finished)

        return {
            "id": row["id"],
            "package": row["package"],
            "versionCode": row["version_code"],
            "versionName": row["version_name"],
            "requestedProvider": row["provider"],
            "succeededProvider": self._provider_from_artifact(row["artifact_path"]),
            "status": status,
            "worker": row["worker"],
            "error": row["error"],
            "providerErrors": _parse_provider_errors(row["provider_errors"]),
            "createdAt": row["created_at"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "updatedAt": row["updated_at"],
            "queueWaitMs": _ms(created, started),
            "downloadMs": _ms(started, finished),
            "elapsedMs": elapsed,
        }

    # ---- provider 流转：成功来源 vs 失败尝试（按错误码） -------------------- #

    def _providers(self, window_jobs: list[sqlite3.Row]) -> dict:
        items: dict[str, dict] = {}

        def ensure(name: str) -> dict:
            return items.setdefault(
                name,
                {"provider": name, "successes": 0, "failures": 0, "errors": {}},
            )

        for row in window_jobs:
            if row["status"] == SUCCEEDED:
                provider = self._provider_from_artifact(row["artifact_path"])
                if provider:
                    ensure(provider)["successes"] += 1
            elif row["status"] == FAILED:
                for err in _parse_provider_errors(row["provider_errors"]):
                    entry = ensure(err.get("provider") or "unknown")
                    entry["failures"] += 1
                    code = err.get("error") or "UNKNOWN"
                    entry["errors"][code] = entry["errors"].get(code, 0) + 1

        result = []
        for entry in items.values():
            attempts = entry["successes"] + entry["failures"]
            entry["successRate"] = round(entry["successes"] / attempts, 4) if attempts else None
            result.append(entry)
        result.sort(key=lambda e: (e["successes"] + e["failures"]), reverse=True)
        return {"items": result}

    # ---- 近 N 天分析：总体成功率 + 按天 + 耗时分布 + 按包 ------------------- #

    def _analytics(self, window_jobs: list[sqlite3.Row], now: datetime, days: int) -> dict:
        terminal = [r for r in window_jobs if r["status"] in (SUCCEEDED, FAILED)]
        succeeded = sum(1 for r in terminal if r["status"] == SUCCEEDED)
        failed = len(terminal) - succeeded

        durations = [
            d
            for r in window_jobs
            if r["status"] == SUCCEEDED
            and (d := _ms(_parse(r["started_at"]), _parse(r["finished_at"]))) is not None
        ]

        return {
            "totals": {
                "total": len(terminal),
                "succeeded": succeeded,
                "failed": failed,
                "successRate": round(succeeded / len(terminal), 4) if terminal else None,
            },
            "daily": _daily_series(terminal, now, days),
            "duration": _duration_stats(durations),
            "perPackage": _per_package_stats(window_jobs),
        }

    # ---- 工具 --------------------------------------------------------------- #

    def _window_jobs(self, conn: sqlite3.Connection, now: datetime, days: int) -> list[sqlite3.Row]:
        # 窗口下界对齐到「最近 days 个自然日（UTC）」的最早一天 00:00，与 _daily_series 的分桶范围同口径，
        # 保证 sum(daily.total) == analytics.totals.total（否则滚动 days*24h 下界会纳入最早桶之外的任务）。
        start_day = now.date() - timedelta(days=days - 1)
        cutoff = datetime(start_day.year, start_day.month, start_day.day, tzinfo=now.tzinfo).isoformat()
        return conn.execute(
            "SELECT status, artifact_path, provider_errors, package, created_at, started_at, finished_at "
            "FROM download_jobs WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()

    def _provider_from_artifact(self, path_str: str | None) -> str | None:
        """从 artifact 路径还原命中来源：`.../artifacts/{provider}/{package}/{version}/file` 的 provider 段。"""
        if not path_str:
            return None
        path = Path(path_str)
        if self.artifacts_dir is not None:
            try:
                rel = path.relative_to(self.artifacts_dir)
                return rel.parts[0] if rel.parts else None
            except ValueError:
                pass
        parts = path.parts
        if "artifacts" in parts:
            index = parts.index("artifacts")
            if index + 1 < len(parts):
                return parts[index + 1]
        return None


# ---- 模块级纯函数（无状态，便于单测） --------------------------------------- #


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds() * 1000.0
    return round(delta, 1) if delta >= 0 else None


def _parse_provider_errors(encoded: str | None) -> list[dict]:
    if not encoded:
        return []
    try:
        data = json.loads(encoded)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (len(sorted_vals) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_vals[low]
    return sorted_vals[low] * (high - rank) + sorted_vals[high] * (rank - low)


def _duration_stats(durations: list[float]) -> dict:
    histogram = [{"label": label, "count": 0} for _, label in _DURATION_BUCKETS]
    for value in durations:
        seconds = value / 1000.0
        for index, (edge, _) in enumerate(_DURATION_BUCKETS):
            if seconds < edge:
                histogram[index]["count"] += 1
                break
    if not durations:
        return {
            "count": 0,
            "avgMs": None,
            "p50Ms": None,
            "p90Ms": None,
            "p95Ms": None,
            "maxMs": None,
            "histogram": histogram,
        }
    ordered = sorted(durations)
    return {
        "count": len(ordered),
        "avgMs": round(sum(ordered) / len(ordered), 1),
        "p50Ms": _percentile(ordered, 0.50),
        "p90Ms": _percentile(ordered, 0.90),
        "p95Ms": _percentile(ordered, 0.95),
        "maxMs": ordered[-1],
        "histogram": histogram,
    }


def _daily_series(terminal_jobs: list[sqlite3.Row], now: datetime, days: int) -> list[dict]:
    """按自然日（UTC）聚合最近 days 天的成功/失败计数，缺失日补零，最早在前。

    按 created_at 分桶（与 _window_jobs 的窗口过滤字段一致），确保每条窗口内任务都落在某个桶里，
    sum(daily.total) 恒等于 analytics.totals.total。
    """
    buckets: dict[str, dict] = {}
    today = now.date()
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        buckets[day] = {"date": day, "total": 0, "succeeded": 0, "failed": 0}
    for row in terminal_jobs:
        stamp = _parse(row["created_at"])
        if stamp is None:
            continue
        day = stamp.date().isoformat()
        bucket = buckets.get(day)
        if bucket is None:
            continue
        bucket["total"] += 1
        if row["status"] == SUCCEEDED:
            bucket["succeeded"] += 1
        else:
            bucket["failed"] += 1
    return list(buckets.values())


def _per_package_stats(window_jobs: list[sqlite3.Row]) -> list[dict]:
    agg: dict[str, dict] = {}
    for row in window_jobs:
        if row["status"] not in (SUCCEEDED, FAILED):
            continue
        entry = agg.setdefault(
            row["package"],
            {"package": row["package"], "total": 0, "succeeded": 0, "failed": 0, "_durations": []},
        )
        entry["total"] += 1
        if row["status"] == SUCCEEDED:
            entry["succeeded"] += 1
            duration = _ms(_parse(row["started_at"]), _parse(row["finished_at"]))
            if duration is not None:
                entry["_durations"].append(duration)
        else:
            entry["failed"] += 1

    result = []
    for entry in agg.values():
        durations = sorted(entry.pop("_durations"))
        entry["successRate"] = round(entry["succeeded"] / entry["total"], 4) if entry["total"] else None
        entry["avgMs"] = round(sum(durations) / len(durations), 1) if durations else None
        entry["p95Ms"] = _percentile(durations, 0.95)
        result.append(entry)
    result.sort(key=lambda e: e["total"], reverse=True)
    return result


# ---- 路由 ------------------------------------------------------------------- #

monitor_router = APIRouter()

_DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
# 图表库本地随服务分发（内网无 CDN）；通过路由静态供给，避免把 1MB 内联进 HTML。
_ECHARTS_PATH = Path(__file__).parent / "echarts.min.js"


def get_monitor_service(settings: Settings = Depends(get_settings)) -> MonitorService:
    return MonitorService(CatalogStore(settings.catalog_db_path), artifacts_dir=settings.artifacts_dir)


@monitor_router.get("/api/v1/monitor/snapshot")
def monitor_snapshot(
    days: int = Query(default=7, ge=1, le=90),
    service: MonitorService = Depends(get_monitor_service),
):
    # 同步 def：FastAPI 自动在 threadpool 执行，避免同步 sqlite3 I/O 阻塞事件循环（面板高频轮询）。
    return JSONResponse(service.snapshot(days=days))


@monitor_router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)


@monitor_router.get("/dashboard/echarts.min.js")
def dashboard_echarts():
    return FileResponse(
        _ECHARTS_PATH,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )
