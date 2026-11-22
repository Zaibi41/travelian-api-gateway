import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.api.routes.operations import router as operations_router
from app.core.config import settings
from app.core.parameter_store import load_parameter_store_settings
from app.core.robots import NoIndexMiddleware
from app.services.flight_payments import run_forever as run_flight_issuance_recovery
from app.services.group_hold_expiry import run_forever as run_group_hold_expiry
from app.vendors.group_ticketing.dhillu_sahi.inventory import warn_if_ids_missing as warn_if_dhillu_sahi_ids_missing


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ready = False
    await asyncio.to_thread(load_parameter_store_settings)
    warn_if_dhillu_sahi_ids_missing()
    from app.services.flight_payments import startup_summary

    logging.getLogger("app.startup").warning(startup_summary())  # e.g. "PayFast mode: UAT, auto-issuing: off"
    app.state.ready = True
    # Group holds: expire past their time limit and give the seats back at the supplier.
    stop = asyncio.Event()
    job = asyncio.create_task(run_group_hold_expiry(stop))
    # Card-paid website flights not issued (or stuck mid-issue after a restart): one retry, or staff alerted.
    recovery = asyncio.create_task(run_flight_issuance_recovery(stop))
    # Supplier balances (travelian#68): Southair read on a schedule (read-only), low-balance alert once per drop.
    from app.services.supplier_wallets import run_forever as run_supplier_balance_checks

    balances = asyncio.create_task(run_supplier_balance_checks(stop))
    try:
        yield
    finally:
        app.state.ready = False
        stop.set()
        for task in (job, recovery, balances):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass


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
if settings.cors_allowed_origins:
    # Exact origins only (validated in Settings). Bearer tokens, no cookies.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        # X-Booking-Token: the website guest's booking token (GET /v1/public/bookings/{ref}, PayFast checkout).
        allow_headers=["Authorization", "Content-Type", "X-Booking-Token"],
        max_age=600,
    )
# Added last, so it is outermost: CORS preflights and error responses get the header too.
app.add_middleware(NoIndexMiddleware)
app.include_router(operations_router)
app.include_router(router, prefix=settings.api_prefix)
