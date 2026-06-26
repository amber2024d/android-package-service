import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.api.discover import discover_router
from app.api.routes import router
from app.catalog.runtime import build_catalog
from app.catalog.scheduler import CatalogRefreshScheduler
from app.core.config import get_settings
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    settings.ensure_directories()

    # 后台定时刷新：进程内调度器（多 worker 用 scheduler_lock 选主，只有一个真正跑）。
    stop = asyncio.Event()
    scheduler_task: asyncio.Task | None = None
    if settings.catalog_refresh_enabled:
        scheduler = CatalogRefreshScheduler(
            build_catalog(settings),
            interval_hours=settings.catalog_refresh_interval_hours,
            lease_seconds=settings.catalog_scheduler_lease_seconds,
        )
        scheduler_task = asyncio.create_task(scheduler.run_forever(stop))

    try:
        yield
    finally:
        stop.set()
        if scheduler_task is not None:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task


app = FastAPI(title="Android Package Service", lifespan=lifespan)
app.include_router(router)
app.include_router(discover_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
