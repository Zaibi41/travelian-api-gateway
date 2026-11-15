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
import re
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Path, Query, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes.admin_dashboard import Viewer, _now, _read_bookings
from app.core.config import settings
from app.core.field_visibility import ISSUE_TICKETS, RECORD_PAYMENTS
from app.db import booking_payments, booking_people, booking_pricing
from app.db import issuance as issuance_store
from app.services import issuance as issuance_service
from app.vendors.flights.southair import normalize as southair_normalize
from app.vendors.flights.southair.client import SouthairError, configured_client
from app.schemas.admin_bookings import (
    BookingDetail, BookingList, BookingMoney, BookingPayment, BookingStatus, Card, DateType, IssuanceChecklist, IssuanceRequest,
    IssuanceResolveRequest, IssuanceResult, IssuanceStatus, PaymentStatus, Product, RecordPaymentRequest, RecordPaymentResponse,
    RevealRequest, RevealResponse, TicketNumber, SellingPriceResult, SetSellingPriceRequest, IssueTicketRequest,
)
from app.schemas.admin_dashboard import Period
from app.services import admin_bookings as service
from app.services import itinerary_pdf as itinerary_pdf_service

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
    if booking is None or not service.visible_to(booking, viewer.visibility):
        raise HTTPException(404, "Booking not found.")
    try:
        people = await run_in_threadpool(booking_people.read, booking.booking_id)
    except (SQLAlchemyError, HTTPException):
        logger.warning("Booking people read failed")
        people = None  # the tabs stay "not available" rather than failing the whole page
    return service.build_detail(booking, viewer.visibility, people, today=_now().date(), can_record_payments=viewer.can(RECORD_PAYMENTS),
                                can_issue_tickets=viewer.can(ISSUE_TICKETS))


@router.get(
    "/{booking_id}/itinerary.pdf", response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "The itinerary, named after the booking reference."},
               403: {"description": "Back-office users only."}, 404: {"description": "No such booking."}},
)
async def itinerary_pdf(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)]):
    """The booking's itinerary / e-ticket as a PDF (Flights and Group Ticketing; app/services/itinerary_pdf.py): status
    banner, Booked by / Contact (Travelian), travelers with fare, Total, Paid and Due (selling price), every flight
    outbound then return. Never a supplier name, group id, cost or profit; a group booking's PNR (the supplier's) only
    with the supplier names permission; full passport numbers only with reveal_identity (each one logged)."""
    bookings = await run_in_threadpool(_read_bookings)
    booking = service.find_booking(bookings, booking_id)
    if booking is None or not service.visible_to(booking, viewer.visibility):
        raise HTTPException(404, "Booking not found.")
    try:
        people = await run_in_threadpool(booking_people.read, booking_id)
    except SQLAlchemyError:
        raise HTTPException(503, "Booking details are not available.") from None
    # Passport numbers: full for viewers allowed to reveal identity (each reveal logged), masked for everyone else.
    travelers = []
    for traveler in (people.travelers if people else []):
        passport = traveler.document_number_masked if traveler.id_type == "PSPT" else None
        if viewer.visibility.reveal_identity and traveler.id_type == "PSPT":
            try:
                revealed = await run_in_threadpool(booking_people.reveal, booking_id, traveler.position, viewer.user_id, "Itinerary PDF")
                passport = revealed.get("document_number") or passport
            except booking_people.NotFound:
                pass
        travelers.append(itinerary_pdf_service.Traveler(traveler.passenger_type, traveler.title, traveler.first_name, traveler.last_name,
                                                        passport, traveler.selling_minor, traveler.ticket_number))
    customer = f"{people.customer.first_name} {people.customer.last_name}".strip() if people and people.customer else booking.customer_name
    paid = sum(payment.amount_minor for payment in people.payments) if people else 0
    document = itinerary_pdf_service.Itinerary(
        reference=booking.booking_id, product=booking.product, pnr=service.visible_pnr(booking, viewer.visibility),
        booking_status=booking.status, issuance_status=booking.ticket_status, payment_status=booking.payment_status,
        created_at=booking.created_at, hold_expires_at=booking.hold_expires_at, currency=booking.currency, total_minor=booking.total_minor,
        paid_minor=paid, travelers=travelers, segments=[segment for segment in (booking.itinerary or []) if isinstance(segment, dict)],
        customer=customer,
    )
    content = await run_in_threadpool(itinerary_pdf_service.render, document, _now())
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", booking.booking_id) + ".pdf"
    return Response(content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                                                     "Cache-Control": "no-store"})


@router.post(
    "/{booking_id}/selling-price", response_model=SellingPriceResult, response_model_exclude_none=True,
    responses={403: {"description": "Needs the record_payments permission."}, 404: {"description": "No such booking."},
               409: {"description": "Not on hold, issued, or a payment is already recorded."},
               422: {"description": "Not whole rupees, below the supplier cost, a different currency, or a passenger type not on the booking."}},
)
async def set_selling_price(viewer: Viewer, request: SetSellingPriceRequest, booking_id: Annotated[str, Path(max_length=64)]):
    """Set what the customer pays (Group Ticketing and Flights): per passenger type, whole rupees, not below cost. Logged."""
    if not viewer.can(RECORD_PAYMENTS):
        raise HTTPException(403, "Changing the selling price needs the record payments permission.")
    currencies = {item.selling_each.currency.upper() for item in request.prices}
    if len(currencies) != 1:
        raise HTTPException(422, "Give every selling price in the booking's currency.")
    prices = {item.passenger_type: item.selling_each.amount_minor for item in request.prices}
    try:
        result = await run_in_threadpool(booking_pricing.set_selling_price, booking_id, prices, viewer.user_id, currencies.pop())
    except booking_pricing.SellingRefused as error:
        raise HTTPException(error.status, error.message) from None
    answer = SellingPriceResult(total=service._money(result.amounts.selling_total_minor, result.currency))
    if viewer.visibility.cost_and_margin and result.amounts.cost_total_minor is not None:
        answer.supplier_cost = service._money(result.amounts.cost_total_minor, result.currency)
        answer.profit = service._money(result.amounts.profit_minor, result.currency)
    return answer


@router.post(
    "/{booking_id}/payments", response_model=RecordPaymentResponse, response_model_exclude_none=True, status_code=201,
    responses={403: {"description": "Needs the record_payments permission."}, 404: {"description": "No such booking."},
               409: {"description": "Closed booking, more than the amount due, or the reference is already recorded."},
               422: {"description": "Invalid payment, wrong currency, or not an active receiving account of this method."}},
)
async def record_payment(viewer: Viewer, request: RecordPaymentRequest, booking_id: Annotated[str, Path(max_length=64)]):
    """Record a customer payment (bank transfer, JazzCash or 1Link). Updates paid / part-paid / unpaid; logged."""
    if not viewer.can(RECORD_PAYMENTS):
        raise HTTPException(403, "Recording payments requires the record payments permission.")
    received_at = request.received_at or _now()
    if received_at.tzinfo is None:
        raise HTTPException(422, "received_at needs a time zone.")
    if received_at > _now() + timedelta(minutes=5):
        raise HTTPException(422, "The payment can't be received in the future.")
    if request.amount.amount_minor <= 0:
        raise HTTPException(422, "The amount must be more than zero.")
    # A group hold: read the supplier's booking first, so a cancellation there refuses the payment.
    from app.services.group_holds import refresh_for_payment
    await refresh_for_payment(booking_id)
    try:
        payment, status = await run_in_threadpool(
            booking_payments.record, booking_id, amount_minor=request.amount.amount_minor, currency=request.amount.currency,
            method=request.method, account_id=request.account_id, payer_reference=(request.reference or "").strip() or None,
            received_at=received_at, actor_user_id=viewer.user_id,
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
    responses={403: {"description": "Needs the reveal_identity permission."}, 404: {"description": "No such booking or traveler."}},
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
    return RevealResponse(**values)


# ---------- issuance ----------

ISSUANCE_RESPONSES = {
    403: {"description": "Needs the issue_tickets permission."}, 404: {"description": "No such booking."},
    409: {"description": "Not issuable now (payment, hold, status, ticketing off, supplier total or balance). Nothing was issued."},
    502: {"description": "The supplier couldn't be checked before sending. Nothing was issued."},
}


def _result(outcome: issuance_service.Outcome) -> IssuanceResult:
    return IssuanceResult(result=outcome.result, issuance_status=outcome.issuance_status, message=outcome.message,
                          tickets=[TicketNumber(position=p, ticket_number=n) for p, n in sorted(outcome.tickets.items())])


async def _run(call):
    try:
        return _result(await call)
    except issuance_store.IssuanceRefused as error:
        if error.code:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": error.message, "code": error.code, **error.extra}, status_code=error.status)
        raise HTTPException(error.status, error.message) from None


@router.get("/{booking_id}/issuance", response_model=IssuanceChecklist, response_model_exclude_none=True, responses=ISSUANCE_RESPONSES)
async def issuance_checklist(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)]):
    """The checks before issuing. Supplier amount, balance and margin only with the cost and margin permission."""
    try:
        target = await run_in_threadpool(issuance_store.load, booking_id)
    except issuance_store.IssuanceRefused as error:
        raise HTTPException(error.status, error.message) from None
    blockers = issuance_store.blockers(target, _now(), settings.ticketing_deadline_margin_seconds)
    status = target.issuance_status
    issuable = status in issuance_store.ISSUABLE and not blockers
    money = lambda minor: BookingMoney(amount_minor=minor, currency=target.currency)  # noqa: E731
    checklist = IssuanceChecklist(
        issuance_status=status, blockers=blockers, full_payment_recorded=target.paid_minor >= target.total_minor,
        amount_paid=money(target.paid_minor), amount_due=money(max(target.total_minor - target.paid_minor, 0)),
        hold_expires_at=target.hold_expires_at, ticketing_enabled=settings.southair_ticketing_enabled,
        can_request=issuable and status == "not_issued",
        can_issue=issuable and viewer.can(ISSUE_TICKETS) and settings.southair_ticketing_enabled,
    )
    if viewer.visibility.cost_and_margin and target.supplier_cost_minor is not None:
        checklist.supplier_amount = money(target.supplier_cost_minor)
        checklist.margin = money(target.total_minor - target.supplier_cost_minor)
        try:
            cap = southair_normalize.parse_amount(((await configured_client().profile()).get("limit") or {}).get("cap"))
            if cap is not None:
                checklist.supplier_balance = BookingMoney(amount_minor=int(cap.value * 100), currency=target.currency)
        except SouthairError:
            pass  # omitted: the supplier didn't answer
    return checklist


@router.post("/{booking_id}/issuance/request", response_model=IssuanceResult, responses=ISSUANCE_RESPONSES)
async def request_issuance(viewer: Viewer, request: IssuanceRequest, booking_id: Annotated[str, Path(max_length=64)]):
    """Ask an approver to issue: needs full payment recorded and a valid hold."""
    return await _run(issuance_service.request(booking_id, viewer, (request.note or "").strip() or None))


@router.post("/{booking_id}/issuance/issue", response_model=IssuanceResult, responses=ISSUANCE_RESPONSES)
async def issue_ticket(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)], request: IssueTicketRequest | None = None):
    """Issue the ticket once (never retried). The result is decided from the supplier's PNR: issued, not_issued or unknown.
    If the airline's price changed, the answer is 409 supplier_cost_changed (or below_cost); send accepted_supplier_cost
    from it to issue at the new cost (the selling price stays)."""
    accepted = request.accepted_supplier_cost.amount_minor if request and request.accepted_supplier_cost else None
    return await _run(issuance_service.issue(booking_id, viewer, accepted_cost_minor=accepted))


@router.post("/{booking_id}/issuance/verify", response_model=IssuanceResult, responses=ISSUANCE_RESPONSES)
async def verify_issuance(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)]):
    """For an unknown issuance: check the PNR with the supplier again (read-only) and settle it if clear."""
    return await _run(issuance_service.verify(booking_id, viewer))


@router.post("/{booking_id}/issuance/resolve", response_model=IssuanceResult, responses={**ISSUANCE_RESPONSES, 422: {"description": "Missing ticket numbers or reason."}})
async def resolve_issuance(viewer: Viewer, request: IssuanceResolveRequest, booking_id: Annotated[str, Path(max_length=64)]):
    """Mark an unknown issuance as issued (ticket number for every traveler) or not issued, with a reason."""
    tickets = {t.position: t.ticket_number.upper() for t in request.ticket_numbers}
    if len(tickets) != len(request.ticket_numbers):
        raise HTTPException(422, "Give one ticket number per traveler.")
    return await _run(issuance_service.resolve(booking_id, viewer, request.outcome, request.reason.strip(), tickets))
