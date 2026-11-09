"""Admin Bookings list and Booking detail (read-only).

Access: back office only (super_admin and staff; agency and customer roles get
403). Field visibility (spec v3.0 D23) is applied in app.services.admin_bookings:
supplier names, supplier cost, margin and the Supplier payment tab are absent
from the response unless the caller holds the matching permission. Filtering by
provider without the supplier names permission is refused (403).
Every response carries `source`; rows from a one-time import are `sample`, and
the list carries `sample_label` "Sample data".
Passengers show date of birth and document number masked. The full values need the
reveal_identity permission and a reason, and every reveal is logged (spec 3.2).
"""

from typing import Annotated

import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Path, Query, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes.admin_dashboard import Viewer, _now, _read_bookings
from app.core import pii
from app.core.config import settings
from app.core.field_visibility import RECORD_PAYMENTS
from app.db import booking_payments, booking_people
from app.schemas.admin_bookings import (
    BookingDetail, BookingList, BookingPayment, BookingStatus, Card, DateType, IssuanceStatus, PaymentStatus, Product,
    RecordPaymentRequest, RecordPaymentResponse, RevealRequest, RevealResponse,
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

logger = logging.getLogger(__name__)


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
    try:
        people = await run_in_threadpool(booking_people.read, booking.booking_id)
    except (SQLAlchemyError, HTTPException):
        logger.warning("Booking people read failed")
        people = None  # the tabs stay "not available" rather than failing the whole page
    return service.build_detail(booking, viewer.visibility, people, today=_now().date(), can_record_payments=viewer.can(RECORD_PAYMENTS))


@router.post(
    "/{booking_id}/payments", response_model=RecordPaymentResponse, response_model_exclude_none=True, status_code=201,
    responses={403: {"description": "Needs the record_payments permission."}, 404: {"description": "No such booking."},
               409: {"description": "Closed booking, more than the amount due, or the reference is already recorded."},
               422: {"description": "Invalid payment or wrong currency."}},
)
async def record_payment(viewer: Viewer, request: RecordPaymentRequest, booking_id: Annotated[str, Path(max_length=64)]):
    """Record a customer payment (bank transfer, JazzCash or 1Link). Updates paid / part-paid / unpaid; logged."""
    if not viewer.can(RECORD_PAYMENTS):
        raise HTTPException(403, "Recording payments requires the record payments permission.")
    if request.received_at.tzinfo is None:
        raise HTTPException(422, "received_at needs a time zone.")
    if request.received_at > _now() + timedelta(minutes=5):
        raise HTTPException(422, "The payment can't be received in the future.")
    if request.amount.amount_minor <= 0:
        raise HTTPException(422, "The amount must be more than zero.")
    try:
        payment, status = await run_in_threadpool(
            booking_payments.record, booking_id, amount_minor=request.amount.amount_minor, currency=request.amount.currency,
            method=request.method, received_into=request.received_into.strip(), payer_reference=request.reference.strip(),
            received_at=request.received_at, actor_user_id=viewer.user_id,
        )
    except booking_payments.PaymentRefused as error:
        raise HTTPException(error.status, error.message) from None
    return RecordPaymentResponse(payment=BookingPayment(
        id=payment.id, amount=service._money(payment.amount_minor, payment.currency), method=payment.method,
        received_into=payment.received_into, reference=payment.reference, received_at=payment.received_at,
        recorded_by_user_id=payment.recorded_by_user_id, recorded_by_role=payment.recorded_by_role, recorded_at=payment.recorded_at,
    ), payment_status=status)


@router.post(
    "/{booking_id}/travelers/{position}/reveal", response_model=RevealResponse, response_model_exclude_none=True,
    responses={403: {"description": "Needs the reveal_identity permission."}, 404: {"description": "No such booking or traveler."},
               503: {"description": "Identity data can't be decrypted right now."}},
)
async def reveal_traveler_identity(
    viewer: Viewer, request: RevealRequest, response: Response,
    booking_id: Annotated[str, Path(max_length=64)], position: Annotated[int, Path(ge=0, le=8)],
):
    """Full date of birth and document number for one traveler. Logged with the reason (spec 3.2)."""
    if not viewer.visibility.reveal_identity:
        raise HTTPException(403, "Revealing identity details requires the reveal identity permission.")
    response.headers["Cache-Control"] = "no-store"
    try:
        values = await run_in_threadpool(booking_people.reveal, booking_id, position, viewer.user_id, request.reason.strip())
    except booking_people.NotFound:
        raise HTTPException(404, "Traveler not found.") from None
    except (pii.PiiNotConfigured, pii.PiiDecryptError):
        logger.error("Identity reveal failed: key not available or value unreadable")
        raise HTTPException(503, "Identity details can't be shown right now.") from None
    return RevealResponse(**values)
