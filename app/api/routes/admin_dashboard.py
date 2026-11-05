"""Admin Dashboard home endpoints (read-only).

Access: local super_admin only (require_local_admin). Agency roles get 403
until bookings carry verified agency ownership. Data: the gateway's own
`bookings` table (app.db.bookings); 503 when the database can't be read.
"""

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from app.core.agency_security import require_local_admin
from app.core.config import settings
from app.schemas.admin_dashboard import (
    AttentionQueue, BookingReport, DashboardFilterOptions, DashboardSummary, Period, Product,
)
from app.services import admin_dashboard as figures
from app.db import bookings as booking_store
from app.db.bookings import BookingRow, BookingsUnavailable

router = APIRouter(
    prefix="/admin",
    tags=["admin dashboard"],
    dependencies=[Depends(require_local_admin)],
    responses={
        403: {"description": "Caller is not a local super_admin."},
        503: {"description": "Dashboard booking data is not available."},
    },
)

ProviderParam = Annotated[str | None, Query(max_length=64, description="Provider name, case-insensitive.")]


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.dashboard_timezone))


def _read_bookings() -> list[BookingRow]:
    try:
        return booking_store.list_dashboard_bookings()
    except BookingsUnavailable:
        raise HTTPException(503, "Dashboard booking data is not available.") from None


def _query(period: Period = "all", product: Product | None = None, provider: str | None = None):
    provider = provider.strip().lower() if provider and provider.strip() else None
    return figures.DashboardQuery(period=period, product=product, provider=provider)


@router.get("/dashboard/summary", response_model=DashboardSummary, response_model_exclude_none=True)
async def dashboard_summary(period: Period = "all", product: Product | None = None, provider: ProviderParam = None):
    bookings = await run_in_threadpool(_read_bookings)
    return figures.build_summary(
        bookings, _query(period, product, provider), _now(),
        window_hours=settings.dashboard_expiring_hold_hours,
        can_see_margin=True,  # super_admin only (see router dependency)
    )


@router.get("/dashboard/booking-report", response_model=BookingReport)
async def dashboard_booking_report(period: Period = "all"):
    bookings = await run_in_threadpool(_read_bookings)
    return figures.build_booking_report(bookings, period, _now())


@router.get("/attention-queue", response_model=AttentionQueue, response_model_exclude_none=True)
async def attention_queue(
    limit: Annotated[int, Query(ge=1, le=50)] = 6, product: Product | None = None, provider: ProviderParam = None,
):
    bookings = await run_in_threadpool(_read_bookings)
    return figures.build_attention_queue(
        bookings, _query("all", product, provider), _now(), settings.dashboard_expiring_hold_hours, limit,
    )


@router.get("/dashboard/filter-options", response_model=DashboardFilterOptions)
async def dashboard_filter_options():
    bookings = await run_in_threadpool(_read_bookings)
    return figures.build_filter_options(bookings)
