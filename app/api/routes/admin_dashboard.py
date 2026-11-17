"""Admin Dashboard home endpoints (read-only).

Access: back office only (super_admin and staff; agency and customer roles get
403). Field visibility (spec v3.0 D23) is applied here, not in the page:
- gross profit (margin) is omitted unless the caller can see cost and margin;
- provider options (supplier names) are omitted, and filtering by provider is
  refused, unless the caller can see supplier names.
Data: the gateway's own `bookings` table (app.db.bookings); 503 when the
database can't be read.
"""

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.field_visibility import BackOfficeUser, require_back_office
from app.schemas.admin_dashboard import (
    AttentionQueue, BookingReport, DashboardFilterOptions, DashboardSummary, Period, Product,
)
from app.services import admin_dashboard as figures
from app.db import bookings as booking_store
from app.db.bookings import BookingRow, BookingsUnavailable

router = APIRouter(
    prefix="/admin",
    tags=["admin dashboard"],
    responses={
        403: {"description": "Caller is not back office, or asked for a field it may not see."},
        503: {"description": "Dashboard booking data is not available."},
    },
)

ProviderParam = Annotated[str | None, Query(max_length=64, description="Provider name, case-insensitive. Requires supplier visibility.")]
Viewer = Annotated[BackOfficeUser, Depends(require_back_office)]


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.dashboard_timezone))


def _read_bookings() -> list[BookingRow]:
    try:
        rows = booking_store.list_dashboard_bookings()
    except BookingsUnavailable:
        raise HTTPException(503, "Dashboard booking data is not available.") from None
    # A supplier hold Travelian couldn't record is not a booking: Bookings shows it to supplier access only.
    return [row for row in rows if row.status != "hold_unrecorded"]


def _query(viewer: BackOfficeUser, period: Period = "all", product: Product | None = None, provider: str | None = None):
    provider = provider.strip().lower() if provider and provider.strip() else None
    if provider and not viewer.visibility.supplier_names:
        raise HTTPException(403, "Filtering by provider requires the supplier names permission.")
    return figures.DashboardQuery(period=period, product=product, provider=provider)


@router.get("/dashboard/summary", response_model=DashboardSummary, response_model_exclude_none=True)
async def dashboard_summary(viewer: Viewer, period: Period = "all", product: Product | None = None, provider: ProviderParam = None):
    query = _query(viewer, period, product, provider)
    bookings = await run_in_threadpool(_read_bookings)
    return figures.build_summary(
        bookings, query, _now(),
        window_hours=settings.dashboard_expiring_hold_hours,
        can_see_margin=viewer.visibility.cost_and_margin,
    )


@router.get("/dashboard/booking-report", response_model=BookingReport)
async def dashboard_booking_report(viewer: Viewer, period: Period = "all"):
    bookings = await run_in_threadpool(_read_bookings)
    return figures.build_booking_report(bookings, period, _now())


@router.get("/attention-queue", response_model=AttentionQueue, response_model_exclude_none=True)
async def attention_queue(
    viewer: Viewer,
    limit: Annotated[int, Query(ge=1, le=50)] = 6, product: Product | None = None, provider: ProviderParam = None,
):
    query = _query(viewer, "all", product, provider)
    bookings = await run_in_threadpool(_read_bookings)
    return figures.build_attention_queue(bookings, query, _now(), settings.dashboard_expiring_hold_hours, limit)


@router.get("/dashboard/filter-options", response_model=DashboardFilterOptions, response_model_exclude_none=True)
async def dashboard_filter_options(viewer: Viewer):
    bookings = await run_in_threadpool(_read_bookings)
    options = figures.build_filter_options(bookings)
    if not viewer.visibility.supplier_names:
        options.providers = None  # supplier names: omitted from the response
    return options
