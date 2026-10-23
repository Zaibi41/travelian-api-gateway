import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.api.routes.operations import router as operations_router
from app.core.config import settings
from app.core.parameter_store import load_parameter_store_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ready = False
    await asyncio.to_thread(load_parameter_store_settings)
    app.state.ready = True
    try:
        yield
    finally:
        app.state.ready = False


app = FastAPI(
    title="Travelian API",
    summary="Flight search API for Travelian's OTA platform.",
    description=(
        "Search flights across configured suppliers through one normalized API. "
        "Supplier searches run concurrently; partial results are returned when "
        "a supplier is unavailable."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.include_router(operations_router)
app.include_router(router, prefix=settings.api_prefix)
