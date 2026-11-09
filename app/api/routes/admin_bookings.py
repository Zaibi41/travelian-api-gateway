"""Admin Bookings list and Booking detail (read-only).

Access: back office only (super_admin and staff; agency and customer roles get
403). Field visibility (spec v3.0 D23) is applied in app.services.admin_bookings:
supplier names, supplier cost, margin and the Supplier payment tab are absent
from the response unless the caller holds the matching permission. Filtering by
provider without the supplier names permission is refused (403).
Every response carries `source`; rows from a one-time import are `sample`, and
the list carries `sample_label` "Sample data".
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.concurrency import run_in_threadpool

from app.api.routes.admin_dashboard import Viewer, _now, _read_bookings
from app.core.config import settings
from app.schemas.admin_bookings import (
    BookingDetail, BookingList, BookingStatus, Card, DateType, IssuanceStatus, PaymentStatus, Product,
)
from app.schemas.admin_dashboard import Period
from app.services import admin_bookings as service

router = APIRouter(
    prefix="/admin/bookings",
    tags=["admin bookings"],
    responses={
        403: {"description": "Caller is not back office, or asked for a field it may not see."},
        503: {"description": "Booking data is not available."},
    },
)

def _clean(value: str | None) -> str | None:
    return value.strip().lower() if value and value.strip() else None


@router.get("", response_model=BookingList, response_model_exclude_none=True)
async def list_bookings(
    viewer: Viewer,
    product: Product | None = None,
    period: Period = "all",
    date_type: DateType = "created",
    q: Annotated[str | None, Query(max_length=64, description="Booking ID or PNR.")] = None,
    booking_status: BookingStatus | None = None,
    payment_status: PaymentStatus | None = None,
    issuance_status: IssuanceStatus | None = None,
    provider: Annotated[str | None, Query(max_length=64, description="Requires the supplier names permission.")] = None,
    agency: Annotated[str | None, Query(max_length=36)] = None,
    card: Annotated[Card | None, Query(description="List only the rows this card counts.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    provider = _clean(provider)
    if provider and not viewer.visibility.supplier_names:
        raise HTTPException(403, "Filtering by provider requires the supplier names permission.")
    query = service.BookingListQuery(
        product=product, period=period, date_type=date_type, q=_clean(q), booking_status=booking_status,
        payment_status=payment_status, issuance_status=issuance_status, provider=provider,
        agency=agency.strip() if agency and agency.strip() else None, card=card, limit=limit, offset=offset,
    )
    bookings = await run_in_threadpool(_read_bookings)
    return service.build_list(bookings, query, _now(), settings.dashboard_expiring_hold_hours, viewer.visibility)


@router.get(
    "/{booking_id}", response_model=BookingDetail, response_model_exclude_none=True,
    responses={404: {"description": "No such booking."}},
)
async def booking_detail(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)]):
    bookings = await run_in_threadpool(_read_bookings)
    booking = service.find_booking(bookings, booking_id)
    if booking is None:
        raise HTTPException(404, "Booking not found.")
    return service.build_detail(booking, viewer.visibility)
