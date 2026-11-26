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
    ActivityItem, AttentionQueue, BookingReport, DashboardFilterOptions, DashboardSummary, Period, Product, RecentActivity,
)
from app.services import admin_dashboard as figures
from app.db import bookings as booking_store
from app.db.bookings import BookingRow, BookingsUnavailable

import logging  # noqa: E402

logger = logging.getLogger(__name__)

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
    return await run_in_threadpool(lambda: figures.build_summary(
        bookings, query, _now(),
        window_hours=settings.dashboard_expiring_hold_hours,
        can_see_margin=viewer.visibility.cost_and_margin, profit_loader=_finance_profits,
    ))


def _finance_profits(sales):
    """Finance profit per sale (travelian#70). Unavailable finance data: the tile falls back to selling - cost."""
    from app.services import finance_reports

    try:
        return finance_reports.profits(sales)
    except Exception:  # noqa: BLE001
        logger.warning("Finance profit couldn't be loaded for the dashboard", exc_info=True)
        return None


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


#: Event names that read better than the plain words (the rest: "payment_recorded" -> "Payment recorded").
ACTIVITY_LABELS = {
    "hold_created": "Hold placed", "hold_requested": "Hold requested", "hold_failed": "Hold failed", "hold_not_recorded": "Hold not recorded",
    "issuance_requested": "Issuance requested", "issuance_started": "Issuing started", "issuance_issued": "Tickets issued",
    "issuance_unknown": "Issuance unknown", "issuance_verified": "Tickets verified", "issuance_aborted": "Issuing stopped, nothing sent",
    "issuance_failed": "Issuing failed", "issuance_found_issued": "Tickets found issued", "issuance_resolved": "Issuance resolved",
    "issuance_checked": "Issuance checked with supplier", "group_ticketed": "Ticketed",
    "final_pnr_recorded": "Final PNR recorded", "ticket_numbers_recorded": "Ticket numbers recorded", "paid_not_ticketed": "Paid, not ticketed",
    "customer_email_not_sent": "Email not sent", "customer_email_bounced": "Email bounced", "selling_price_set": "Selling price set",
    "online_payment_started": "Online payment started", "online_payment_failed": "Online payment failed",
    "online_payment_needs_review": "Online payment to review", "payment_slip_uploaded": "Payment slip uploaded",
}


def activity_label(action: str) -> str:
    return ACTIVITY_LABELS.get(action) or action.replace("_", " ").capitalize()


@router.get("/recent-activity", response_model=RecentActivity, response_model_exclude_none=True)
async def recent_activity(viewer: Viewer, limit: Annotated[int, Query(ge=1, le=50)] = 8) -> RecentActivity:
    """gateway#87: the latest booking events across bookings (who, what, which booking, when), newest first. Back office only.
    Event details are never sent; a booking Travelian couldn't record shows only with the supplier names permission."""
    try:
        rows = await run_in_threadpool(booking_store.recent_activity, limit, viewer.visibility.supplier_names)
    except BookingsUnavailable:
        raise HTTPException(503, "Recent activity is not available.") from None
    return RecentActivity(items=[ActivityItem(at=r.at, action=r.action, label=activity_label(r.action), booking_id=r.booking_id, product=r.product,
                                              actor_user_id=r.actor_user_id, actor_role=r.actor_role) for r in rows])
