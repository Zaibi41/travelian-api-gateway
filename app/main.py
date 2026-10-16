from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Shared async HTTP client can be added here as vendor adapters are integrated.
    yield


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
app.include_router(router, prefix=settings.api_prefix)
