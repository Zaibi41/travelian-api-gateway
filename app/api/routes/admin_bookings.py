"""Admin Bookings list and Booking detail (read-only).

Access: back office only (super_admin and staff; agency and customer roles get
403). Field visibility (spec v3.0 D23) is applied in app.services.admin_bookings:
supplier names, supplier cost, margin and the Supplier payment tab are absent
from the response unless the caller holds the matching permission. Filtering by
provider without the supplier names permission is refused (403).
Every response carries `source`; rows from a one-time import are `sample`, and
the list carries `sample_label` "Sample data".
Passengers carry their full date of birth, document number and expiry, as stored.
"""

from typing import Annotated, Literal

import logging
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes.admin_dashboard import Viewer, _now, _read_bookings
from app.core.config import settings
from app.core.field_visibility import ISSUE_TICKETS, RECORD_PAYMENTS, BackOfficeUser, require_super_admin
from app.db import booking_corrections, booking_notes, booking_payments, booking_people, booking_pricing
from app.db import issuance as issuance_store
from app.services.group_ticketing_status import ITINERARY_NOT_YET, ticketing_status
from app.services import group_terms
from app.services import issuance as issuance_service
from app.vendors.flights.southair import normalize as southair_normalize
from app.vendors.flights.southair.client import SouthairError, configured_client
from app.schemas.admin_bookings import AddBookingNote, BookingNote, BookingNotes, CorrectionResult, CustomerNameCorrection, TravelerCorrection
from app.schemas.admin_bookings import (
    BookingDetail, BookingList, BookingMoney, ResendEmailResult, CustomerEmailChange, CustomerEmailChanged, CustomerContactChange, CustomerContactChanged, BookingPayment, BookingStatus, Card, DateType, IssuanceAttempt, IssuanceChecklist, IssuanceRequest, ReholdBody, ReholdResult, SupplierBookingView,
    IssuanceResolveRequest, IssuanceResult, IssuanceStatus, PaymentStatus, Product, RecordPaymentRequest, RecordPaymentResponse,
    TicketNumber, SellingPriceResult, SetSellingPriceRequest, IssueTicketRequest,
)
from app.schemas.admin_dashboard import Period
from app.services import admin_bookings as service
from app.services import itinerary_pdf as itinerary_pdf_service
from app.services import sooperfare_bookings

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
    detail = service.build_detail(booking, viewer.visibility, people, today=_now().date(), can_record_payments=viewer.can(RECORD_PAYMENTS),
                                  can_issue_tickets=viewer.can(ISSUE_TICKETS))
    detail.can_correct = viewer.role == "super_admin"
    try:
        supplier = await run_in_threadpool(sooperfare_bookings.view, booking.booking_id, viewer.visibility.supplier_names)
    except Exception:  # noqa: BLE001 - the SooperFare block is information only; the booking still shows
        logger.warning("SooperFare booking block not read")
        supplier = None
    if supplier:
        supplier["can_cancel"] = supplier["can_cancel"] and viewer.can(ISSUE_TICKETS)
        supplier["can_rehold"] = supplier["can_rehold"] and viewer.can(ISSUE_TICKETS)
        detail.supplier_booking = SupplierBookingView(**supplier)
    return detail


@router.post("/{booking_id}/supplier-cancel", response_model=SupplierBookingView,
             responses={403: {"description": "Needs the issue tickets permission."}, 409: {"description": "Not a held SooperFare booking."},
                        502: {"description": "SooperFare refused or didn't answer; nothing changed."}})
async def cancel_at_supplier(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)]):
    """Cancel a held SooperFare booking at SooperFare (staff confirm first in the admin). The booking is marked Cancelled
    only when SooperFare's own record says so; otherwise it shows "Cancel requested". Logged in the audit trail."""
    return await _supplier_action(viewer, booking_id, sooperfare_bookings.cancel)


@router.post("/{booking_id}/supplier-check", response_model=SupplierBookingView,
             responses={404: {"description": "No SooperFare reservation."}, 502: {"description": "SooperFare couldn't be read."}})
async def check_at_supplier(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)]):
    """Read-only: what SooperFare shows for this booking now; marks it Cancelled once SooperFare confirms a requested cancel."""
    return await _supplier_action(viewer, booking_id, sooperfare_bookings.check)


@router.post("/{booking_id}/supplier-rehold", response_model=ReholdResult,
             responses={403: {"description": "Needs the issue tickets and supplier names permissions."},
                        409: {"description": "Not re-holdable (still held at SooperFare, issued, not SooperFare), or price_changed: "
                                           "the body has new_total and old_total; send accept_total_minor to hold at the new price."},
                        502: {"description": "SooperFare couldn't be checked or didn't hold it; nothing was booked."}})
async def rehold_at_supplier(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)], body: ReholdBody | None = None):
    """Re-hold at SooperFare: the same flights and fare again for a booking whose hold is gone (expired, or paid but not
    ticketed), with the saved travelers. Same selling total only, unless staff accept the new total shown in a 409
    price_changed. Only after SooperFare's own record shows the old reservation is no longer active. Logged in the audit trail."""
    if not viewer.can(ISSUE_TICKETS) or not viewer.visibility.supplier_names:
        raise HTTPException(403, "Re-holding at the supplier needs the issue tickets and supplier names permissions.")
    from fastapi.responses import JSONResponse

    from app.services import flight_fallback

    try:
        what = await flight_fallback.staff_rehold(booking_id, viewer.user_id, (body or ReholdBody()).accept_total_minor)
    except issuance_store.IssuanceRefused as error:
        return JSONResponse({"detail": error.message, "code": "not_found"}, status_code=error.status)  # type: ignore[return-value]
    except flight_fallback.Rehold as error:
        return JSONResponse({"detail": error.message, "code": error.code, **error.extra}, status_code=error.status)  # type: ignore[return-value]
    return ReholdResult(message=what)


@router.get("/{booking_id}/supplier-confirm-preview", responses={404: {"description": "No SooperFare reservation."}})
async def supplier_confirm_preview(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)]):
    """The exact request Travelian would send to SooperFare to issue this booking. Read-only: nothing is sent."""
    if not viewer.can(ISSUE_TICKETS) or not viewer.visibility.supplier_names or not viewer.visibility.cost_and_margin:
        raise HTTPException(403, "Needs the issue tickets, supplier names and cost permissions.")
    try:
        return await run_in_threadpool(sooperfare_bookings.confirm_request, booking_id)
    except sooperfare_bookings.SupplierCancelError as error:
        raise HTTPException(error.status, error.message) from None


async def _supplier_action(viewer, booking_id: str, action) -> SupplierBookingView:
    if not viewer.can(ISSUE_TICKETS) or not viewer.visibility.supplier_names:
        raise HTTPException(403, "Cancelling at the supplier needs the issue tickets and supplier names permissions.")
    try:
        await action(booking_id, viewer.user_id)
    except sooperfare_bookings.SupplierCancelError as error:
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": error.message, "code": error.code}, status_code=error.status)  # type: ignore[return-value]
    view = await run_in_threadpool(sooperfare_bookings.view, booking_id, True)
    return SupplierBookingView(**{**view, "can_cancel": view["can_cancel"] and viewer.can(ISSUE_TICKETS),
                                  "can_rehold": view["can_rehold"] and viewer.can(ISSUE_TICKETS)})  # type: ignore[arg-type]


@router.get(
    "/{booking_id}/itinerary.pdf", response_class=Response,
    responses={200: {"content": {"application/pdf": {}, "image/png": {}}, "description": "The itinerary, named after the booking reference."},
               403: {"description": "Back-office users only."}, 404: {"description": "No such booking."},
               409: {"description": "A group booking that isn't Ticketed yet (final PNR and every ticket number)."}},
)
async def itinerary_pdf(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)],
                        format: Annotated[Literal["pdf", "png"], Query(description="pdf (default) or png: the same document as one image.")] = "pdf",
                        fare: Annotated[bool, Query(description="false: print without fare (no fare column, no Total / Paid / Due), issue #18.")] = True):
    """The booking's itinerary / e-ticket as a PDF (Flights and Group Ticketing; app/services/itinerary_pdf.py): status
    banner, Booked by / Contact (Travelian), travelers with fare, Total, Paid and Due (selling price), every flight
    outbound then return. Never a supplier name, group id, cost or profit; full passport numbers. A group booking prints
    only once Ticketed, with the airline's final PNR (409 before).
    format=png: the finished PDF rendered at about 150 DPI, pages stacked into one image (same rules)."""
    bookings = await run_in_threadpool(_read_bookings)
    booking = service.find_booking(bookings, booking_id)
    if booking is None or not service.visible_to(booking, viewer.visibility):
        raise HTTPException(404, "Booking not found.")
    try:
        people = await run_in_threadpool(booking_people.read, booking_id)
    except SQLAlchemyError:
        raise HTTPException(503, "Booking details are not available.") from None
    travelers = []
    for traveler in (people.travelers if people else []):
        passport = traveler.document_number if traveler.id_type == "PSPT" else None
        travelers.append(itinerary_pdf_service.Traveler(traveler.passenger_type, traveler.title, traveler.first_name, traveler.last_name,
                                                        passport, traveler.selling_minor, traveler.ticket_number))
    customer = f"{people.customer.first_name} {people.customer.last_name}".strip() if people and people.customer else booking.customer_name
    paid = sum(payment.amount_minor for payment in people.payments) if people else 0
    pnr = service.visible_pnr(booking, viewer.visibility)
    if booking.product == "group":
        # A group itinerary / e-ticket prints only once Ticketed, with the airline's final PNR.
        if ticketing_status(booking.status, booking.ticket_status, booking.final_pnr, paid >= (booking.total_minor or 0)) != "ticketed":
            raise HTTPException(409, ITINERARY_NOT_YET)
        pnr = booking.final_pnr
    document = itinerary_pdf_service.Itinerary(
        reference=booking.booking_id, product=booking.product, pnr=pnr,
        booking_status=booking.status, issuance_status=booking.ticket_status, payment_status=booking.payment_status,
        created_at=booking.created_at, hold_expires_at=booking.hold_expires_at, currency=booking.currency, total_minor=booking.total_minor,
        paid_minor=paid, travelers=travelers, segments=[segment for segment in (booking.itinerary or []) if isinstance(segment, dict)],
        customer=customer, show_fare=fare, card_fee_minor=sum(getattr(payment, "card_fee_minor", 0) for payment in people.payments) if people else 0,
    )
    if group_terms.acknowledgement(getattr(people, "events", None)) is not None:
        # The booking carries the stored group fare terms: the tag next to the fare and the full line in the terms box.
        document.fare_note, document.terms = group_terms.TAG, [group_terms.TERM]
    content = await run_in_threadpool(itinerary_pdf_service.render, document, _now())
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", booking.booking_id)
    suffix = "" if fare else "-no-fare"
    media, filename = "application/pdf", f"{safe}{suffix}.pdf"
    if format == "png":
        content = await run_in_threadpool(itinerary_pdf_service.to_png, content)
        media, filename = "image/png", f"{safe}-itinerary{suffix}.png"
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"',
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
    if status == "paid":
        from app.services import customer_emails

        method = {"bank_transfer": "bank transfer", "jazzcash": "JazzCash", "1link": "1Link"}.get(payment.method, payment.method)
        await customer_emails.send(booking_id, "payment_received", method=method)
    return RecordPaymentResponse(payment=BookingPayment(
        id=payment.id, amount=service._money(payment.amount_minor, payment.currency), method=payment.method,
        received_into=payment.received_into, reference=payment.reference, received_at=payment.received_at,
        recorded_by_user_id=payment.recorded_by_user_id, recorded_by_role=payment.recorded_by_role, recorded_at=payment.recorded_at,
    ), payment_status=status)


# ---------- issuance ----------

ISSUANCE_RESPONSES = {
    403: {"description": "Needs the issue_tickets permission."}, 404: {"description": "No such booking."},
    409: {"description": "Not issuable now (payment, hold, status, ticketing off, supplier total or balance). Nothing was issued."},
    502: {"description": "The supplier couldn't be checked before sending. Nothing was issued."},
}


@router.post("/{booking_id}/emails/{email}/resend", response_model=ResendEmailResult,
             responses={404: {"description": "No such booking."}, 409: {"description": "That email was never sent for this booking."},
                        422: {"description": "Not a customer email."}})
async def resend_email(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)], email: Annotated[str, Path(max_length=40)]):
    """Send a customer email for this booking again (gateway#53), e.g. after SES refused it. Back office only; logged
    on the booking with who resent it. Sending can still fail; the answer says why in plain words."""
    from app.services import customer_emails

    bookings = await run_in_threadpool(_read_bookings)
    booking = service.find_booking(bookings, booking_id)
    if booking is None or not service.visible_to(booking, viewer.visibility):
        raise HTTPException(404, "Booking not found.")
    try:
        sent, message = await run_in_threadpool(customer_emails.resend, booking.booking_id, email, viewer.user_id)
    except customer_emails.ResendRefused as error:
        raise HTTPException(error.status, error.message) from None
    return ResendEmailResult(sent=sent, message=message)


@router.put("/{booking_id}/customer-email", response_model=CustomerEmailChanged,
            responses={404: {"description": "No such booking."}, 409: {"description": "Same address, or no customer record."},
                       422: {"description": "Not a valid email address."}})
async def change_customer_email(viewer: Viewer, request: CustomerEmailChange, booking_id: Annotated[str, Path(max_length=64)]):
    """Fix the customer's email on this booking after a bounce or complaint (gateway#59). Back office; logged (masked).
    Later emails go to the new address; Resend sends the bounced ones again."""
    from app.services import customer_emails

    bookings = await run_in_threadpool(_read_bookings)
    booking = service.find_booking(bookings, booking_id)
    if booking is None or not service.visible_to(booking, viewer.visibility):
        raise HTTPException(404, "Booking not found.")
    try:
        email = await run_in_threadpool(customer_emails.change_address, booking.booking_id, request.email, viewer.user_id)
    except customer_emails.ResendRefused as error:
        raise HTTPException(error.status, error.message) from None
    return CustomerEmailChanged(email=email, message="Email address updated. Resend the emails the customer missed.")


@router.put("/{booking_id}/customer-contact", response_model=CustomerContactChanged,
            responses={404: {"description": "No such booking."}, 422: {"description": "Not a valid phone or email."}})
async def change_customer_contact(viewer: Viewer, request: CustomerContactChange, booking_id: Annotated[str, Path(max_length=64)]):
    """Change the customer's mobile number and email on this booking (travelian#56), e.g. on the Payments tab before the
    payment is recorded. No re-hold: the airline hold and the price stay. Back office; logged (masked)."""
    from app.services import booking_contact

    bookings = await run_in_threadpool(_read_bookings)
    booking = service.find_booking(bookings, booking_id)
    if booking is None or not service.visible_to(booking, viewer.visibility):
        raise HTTPException(404, "Booking not found.")
    try:
        saved = (await run_in_threadpool(lambda: booking_contact.change(
            booking.booking_id, email=request.email, phone_code=request.phone_code, phone=request.phone,
            actor_user_id=viewer.user_id, website=False))).contact
    except booking_contact.ContactRefused as error:
        raise HTTPException(error.status, error.message) from None
    return CustomerContactChanged(phone=f"{saved.phone_code} {saved.phone}", email=saved.email, message="Contact details saved.")


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
        # D57: a difference accepted with a reason counts as paid; the amount due leaves it out.
        issuance_status=status, blockers=blockers, full_payment_recorded=target.paid_minor + target.shortfall_minor >= target.total_minor,
        amount_paid=money(target.paid_minor), amount_due=money(max(target.total_minor - target.paid_minor - target.shortfall_minor, 0)),
        hold_expires_at=target.hold_expires_at, ticketing_enabled=issuance_service.ticketing_on(target.provider, target.product),
        # One reason at a time (gateway#44): while a flight's hold is gone, the hold is the reason, not the ticketing switch.
        ticketing_note=None if target.product == "flight" and issuance_store.hold_blocker(target, _now(), settings.ticketing_deadline_margin_seconds)
        else issuance_service.ticketing_reason(target.provider, target.product),
        can_request=issuable and status == "not_issued",
        can_issue=issuable and viewer.can(ISSUE_TICKETS) and issuance_service.ticketing_on(target.provider, target.product),
    )
    try:
        attempt = await issuance_service.last_attempt(target)
    except Exception:  # noqa: BLE001 - the last attempt is information only; the checks above still show
        logger.warning("Last issuance attempt couldn't be read", exc_info=True)
        attempt = None
    if attempt is not None:
        checklist.last_attempt = IssuanceAttempt(at=attempt.at, outcome=attempt.outcome, reason=attempt.reason,
                                                 supplier_answer=attempt.supplier_answer, pnr_status=attempt.pnr_status)
        checklist.can_check = attempt.can_check and viewer.can(ISSUE_TICKETS) and target.provider in (None, *issuance_service.ISSUING_PROVIDERS)
    try:  # travelian#68: warn when the supplier's known balance is low or below this ticket's cost (information only)
        from app.services import supplier_wallets

        checklist.balance_warning = await run_in_threadpool(supplier_wallets.issue_warning, target.provider, target.supplier_cost_minor,
                                                            viewer.visibility.cost_and_margin and viewer.visibility.supplier_names)
    except Exception:  # noqa: BLE001
        logger.warning("Supplier balance warning couldn't be worked out", exc_info=True)
    if viewer.visibility.cost_and_margin and target.supplier_cost_minor is not None:
        checklist.supplier_amount = money(target.supplier_cost_minor)
        billable = await run_in_threadpool(_billable_minor, target.id)
        if billable is not None:  # issue #202: the supplier billable amount is the one figure for what the supplier charges
            checklist.supplier_amount = money(billable)
        checklist.margin = money(target.total_minor - target.supplier_cost_minor)
        try:
            # Southair's balance only for a Southair booking: it says nothing about another supplier's account.
            cap = None if (target.provider or "southair") != "southair" else southair_normalize.parse_amount(((await configured_client().profile()).get("limit") or {}).get("cap"))
            if cap is not None:
                checklist.supplier_balance = BookingMoney(amount_minor=int(cap.value * 100), currency=target.currency)
        except SouthairError:
            pass  # omitted: the supplier didn't answer
    return checklist


@router.post("/{booking_id}/issuance/request", response_model=IssuanceResult, responses=ISSUANCE_RESPONSES)
async def request_issuance(viewer: Viewer, request: IssuanceRequest, booking_id: Annotated[str, Path(max_length=64)]):
    """Ask an approver to issue: needs full payment recorded and a valid hold."""
    return await _run(issuance_service.request(booking_id, viewer, (request.note or "").strip() or None))


async def _ticket_email(result, booking_id: str):
    """The customer's e-ticket email once a booking is issued (sent once per booking)."""
    if isinstance(result, IssuanceResult) and result.issuance_status == "issued":
        from app.services import customer_emails

        await customer_emails.send(booking_id, "ticket_issued")
    return result


@router.post("/{booking_id}/issuance/issue", response_model=IssuanceResult, responses=ISSUANCE_RESPONSES)
async def issue_ticket(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)], request: IssueTicketRequest | None = None):
    """Issue the ticket once (never retried). The result is decided from the supplier's PNR: issued, not_issued or unknown.
    If the airline's price changed, the answer is 409 supplier_cost_changed (or below_cost); send accepted_supplier_cost
    from it to issue at the new cost (the selling price stays)."""
    accepted = request.accepted_supplier_cost.amount_minor if request and request.accepted_supplier_cost else None
    return await _ticket_email(await _run(issuance_service.issue(booking_id, viewer, accepted_cost_minor=accepted)), booking_id)


@router.post("/{booking_id}/issuance/verify", response_model=IssuanceResult, responses=ISSUANCE_RESPONSES)
async def verify_issuance(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)]):
    """Check with supplier (read-only) after an unknown or failed issuance: tickets found -> recorded; no ticket and
    nothing paid -> safe to issue again; unclear -> unknown. Never sends a ticketing request."""
    return await _ticket_email(await _run(issuance_service.verify(booking_id, viewer)), booking_id)


@router.post("/{booking_id}/issuance/resolve", response_model=IssuanceResult, responses={**ISSUANCE_RESPONSES, 422: {"description": "Missing ticket numbers or reason."}})
async def resolve_issuance(viewer: Viewer, request: IssuanceResolveRequest, booking_id: Annotated[str, Path(max_length=64)]):
    """Mark an unknown issuance as issued (ticket number for every traveler) or not issued, with a reason."""
    tickets = {t.position: t.ticket_number.upper() for t in request.ticket_numbers}
    if len(tickets) != len(request.ticket_numbers):
        raise HTTPException(422, "Give one ticket number per traveler.")
    return await _ticket_email(await _run(issuance_service.resolve(booking_id, viewer, request.outcome, request.reason.strip(), tickets)), booking_id)


def _billable_minor(booking_id: str) -> int | None:
    """Issue #202: the booking's supplier billable amount, or None (not set, before migration 0037, or unreadable)."""
    from fastapi import HTTPException
    from sqlalchemy.exc import SQLAlchemyError

    from app.db import agencies, finance_ops, online_attempts

    try:
        with agencies.application_engine().begin() as connection:
            if not online_attempts.has_finance_ops(connection):
                return None
            found = finance_ops.billing(booking_id, connection)
    except (HTTPException, SQLAlchemyError):
        return None
    return found.billable_minor if found else None


# ---------- Super Admin corrections (travelian#225) ----------

SuperAdmin = Annotated[BackOfficeUser, Depends(require_super_admin)]
CORRECTION_RESPONSES = {403: {"description": "Super Admin only."}, 404: {"description": "No such booking or traveler."},
                        409: {"description": "Nothing changed, or no customer record."}, 422: {"description": "Invalid value or no reason."}}


def _corrected(result: booking_corrections.Corrected) -> CorrectionResult:
    message = "Corrected and logged."
    if result.sent_to_airline:
        message += " This booking was already sent to the airline: correct it with the airline too."
    return CorrectionResult(fields=result.fields, sent_to_airline=result.sent_to_airline, message=message)


@router.put("/{booking_id}/travelers/{position}", response_model=CorrectionResult, responses=CORRECTION_RESPONSES)
async def correct_traveler(viewer: SuperAdmin, body: TravelerCorrection, booking_id: Annotated[str, Path(max_length=64)],
                           position: Annotated[int, Path(ge=0, le=20)]):
    """Correct a traveler's title, name, date of birth or document on this booking. Super Admin only; needs a reason;
    logged on the booking (fields and reason, never values). Prices, payments and tickets are not changed."""
    changes = body.model_dump(exclude={"reason"}, exclude_none=True)
    try:
        result = await run_in_threadpool(booking_corrections.correct_traveler, booking_id, position, changes, body.reason, viewer.user_id)
    except booking_corrections.CorrectionRefused as error:
        raise HTTPException(error.status, error.message) from None
    return _corrected(result)


@router.put("/{booking_id}/customer-name", response_model=CorrectionResult, responses=CORRECTION_RESPONSES)
async def correct_customer_name(viewer: SuperAdmin, body: CustomerNameCorrection, booking_id: Annotated[str, Path(max_length=64)]):
    """Correct the customer's name on this booking. Super Admin only; needs a reason; logged."""
    changes = body.model_dump(exclude={"reason"}, exclude_none=True)
    try:
        result = await run_in_threadpool(booking_corrections.correct_customer, booking_id, changes, body.reason, viewer.user_id)
    except booking_corrections.CorrectionRefused as error:
        raise HTTPException(error.status, error.message) from None
    return _corrected(result)


# ---------- notes (gateway#89) ----------

NOTE_RESPONSES = {403: {"description": "Back office only."}, 404: {"description": "No such booking."}, 422: {"description": "Empty or too long."}}


def _note_view(note: booking_notes.Note) -> BookingNote:
    return BookingNote(id=note.id, body=note.body, created_at=note.created_at, created_by_user_id=note.created_by_user_id,
                       created_by_role=note.created_by_role)


async def _visible_booking(viewer, booking_id: str):
    bookings = await run_in_threadpool(_read_bookings)
    booking = service.find_booking(bookings, booking_id)
    if booking is None or not service.visible_to(booking, viewer.visibility):
        raise HTTPException(404, "Booking not found.")
    return booking


@router.get("/{booking_id}/notes", response_model=BookingNotes, response_model_exclude_none=True, responses=NOTE_RESPONSES)
async def list_booking_notes(viewer: Viewer, booking_id: Annotated[str, Path(max_length=64)]):
    """Staff notes on this booking, newest first. Back office only."""
    booking = await _visible_booking(viewer, booking_id)
    try:
        notes = await run_in_threadpool(booking_notes.list_notes, booking.booking_id)
    except booking_notes.NoteRefused as error:
        raise HTTPException(error.status, error.message) from None
    return BookingNotes(notes=[_note_view(n) for n in notes])


@router.post("/{booking_id}/notes", response_model=BookingNote, response_model_exclude_none=True, status_code=201, responses=NOTE_RESPONSES)
async def add_booking_note(viewer: Viewer, body: AddBookingNote, booking_id: Annotated[str, Path(max_length=64)]):
    """Add a note (up to 1,000 characters). Back office only; also logged on the Audit tab as "Note added" (without the text)."""
    booking = await _visible_booking(viewer, booking_id)
    try:
        note = await run_in_threadpool(booking_notes.add_note, booking.booking_id, body.body, viewer.user_id)
    except booking_notes.NoteRefused as error:
        raise HTTPException(error.status, error.message) from None
    return _note_view(note)
