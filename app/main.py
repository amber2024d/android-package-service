from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.discover import discover_router
from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    settings.ensure_directories()
    yield


app = FastAPI(title="Android Package Service", lifespan=lifespan)
app.include_router(router)
app.include_router(discover_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
