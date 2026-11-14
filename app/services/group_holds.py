"""Group Ticketing hold lifecycle (admin, adults only): hold seats -> price shown -> payments ->
price re-checked -> issue tickets; or release; or the hold expires.

Each group source plugs in as a GroupHoldProvider. The provider registry is empty today: no
configured group supplier has a documented hold / release / price / issue contract with response
schemas (docs/api/group-holds.md), so `provider_for` returns None and holds are refused with
"hold_not_supported" before anything is sent. Supplier calls that change state are never retried.

The price is never known before the hold. The hold's supplier price per adult becomes the selling
price (group markup) and the booking total. Before issuing, the price is checked again: when it
moved, issuing stops with "price_changed" until the customer agrees (accepted_total) or the hold
is released. Supplier names and cost never leave this module except for viewers allowed to see them.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol

from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from app.core import pii
from app.core.config import settings
from app.core.field_visibility import BACK_OFFICE_ROLES, ISSUE_TICKETS, RECORD_PAYMENTS, BackOfficeUser
from app.db import booking_people, group_holds as store, saved_people, supplier_bookings
from app.db import issuance as issuance_store
from app.schemas.group_holds import (
    ContactError, GroupHoldCustomer, GroupHoldFlight, GroupHoldPayment, GroupHoldRequest, GroupHoldTraveler, GroupHoldView,
    GroupIssueResult, GroupPriceCheck, age_on,
)
from app.schemas.group_ticketing import GroupInventory, GroupTicket
from app.schemas.live_flights import HoldContact, HoldPassenger
from app.services import flight_holds
from app.services.group_ticketing import TicketNotFound, find_ticket, sector_codes, stock_label
from app.services.group_ticketing_view import GroupMoney, _departure_time, selling_price
from app.services.live_flights import money_text
from app.services import supplier_logos

logger = logging.getLogger(__name__)
REFERENCE_PREFIX = "TRV-GRP-"


# ---------- providers ----------

@dataclass(frozen=True)
class HeldSeats:
    """What a provider returns for a confirmed hold."""
    pnr: str
    supplier_ref: str          # the supplier's hold or booking id; stays in the gateway
    expires_at: datetime       # timezone-aware
    cost_per_adult: GroupMoney  # supplier price per adult

    def __repr__(self) -> str:
        return f"HeldSeats(pnr={self.pnr})"


@dataclass(frozen=True)
class HeldRef:
    pnr: str | None
    supplier_ref: str
    adults: int


class SupplierRefused(Exception):
    """The supplier answered clearly that it did nothing (no hold, nothing released, nothing issued)."""


class GroupHoldProvider(Protocol):
    async def hold(self, ticket: GroupTicket, passengers: list[HoldPassenger], contact: HoldContact) -> HeldSeats: ...
    async def price(self, held: HeldRef) -> GroupMoney: ...
    async def release(self, held: HeldRef) -> None: ...
    async def issue(self, held: HeldRef, cost_per_adult: GroupMoney) -> list[str]: ...


#: Group source (GroupTicket.vendor) -> provider factory. Add a source only with its supplier's
#: documented hold, release, price and issue contracts (gateway AGENTS.md). See SUPPORT.
PROVIDERS: dict[str, Callable[[], GroupHoldProvider]] = {}

#: What each configured group source supports today (reported, not used for routing).
SUPPORT = {
    "dhillu_sahi": "Inventory, read-only availability check, add-booking (kept behind booking authorization) and booking details. "
                   "No hold expiry, release, price re-check or ticket issuance contract.",
    "skypass": "Inventory. Its collection lists hold-available-seats, release-seats, submit-booking, cancel-booking and booking "
               "details, but no response formats, hold expiry or price fields, so no adapter is built.",
}


def provider_for(vendor: str | None) -> GroupHoldProvider | None:
    factory = PROVIDERS.get(vendor or "")
    return factory() if factory else None


# ---------- errors ----------

class GroupHoldError(Exception):
    """status, the sentence to show, a stable code; field_errors [(path, message)]; extra facts (pnr, booking_id, totals)."""

    def __init__(self, status: int, message: str, code: str, field_errors: list[tuple[list, str]] | None = None, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.field_errors = field_errors or []
        self.extra = extra


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _markup_configured() -> bool:
    return selling_price(GroupMoney(amount_minor=100, currency=settings.group_ticketing_default_currency)) is not None


# ---------- read ----------

def _status(booking: store.GroupBooking, now: datetime) -> str:
    if booking.issuance_status in ("processing", "unknown"):
        return booking.issuance_status
    if booking.booking_status == "ticket_issued" or booking.issuance_status == "issued":
        return "issued"
    if booking.booking_status == "cancelled":
        return "released"
    if booking.booking_status == "expired" or (booking.booking_status == "held" and store.is_over(booking, now)):
        return "expired"
    return "held"


def _flight(booking: store.GroupBooking) -> GroupHoldFlight:
    segment = booking.itinerary[0] if booking.itinerary else {}
    departure = str(segment.get("departure_local") or "")
    return GroupHoldFlight(
        airline=segment.get("airline_name"), origin=segment.get("origin"), destination=segment.get("destination"),
        sector=f"{segment['origin']} - {segment['destination']}" if segment.get("origin") and segment.get("destination") else None,
        departure_date=departure[:10] or None, departure_time=departure[11:16] or None,
        logo_url=supplier_logos.register(segment.get("logo")) if segment.get("logo") else None, baggage=segment.get("baggage"),
    )


def build_view(booking: store.GroupBooking, people: booking_people.People | None, viewer: BackOfficeUser, now: datetime) -> GroupHoldView:
    money = lambda minor: GroupMoney(amount_minor=minor, currency=booking.currency)  # noqa: E731
    status = _status(booking, now)
    due = max(booking.total_minor - booking.paid_minor, 0)
    adults = max(booking.adults, 1)
    open_hold = status == "held"
    view = GroupHoldView(
        booking_id=booking.reference, pnr=booking.pnr, status=status, hold_expires_at=booking.hold_expires_at, adults=booking.adults,
        flight=_flight(booking), stock=stock_label(booking.provider or ""),
        price_per_adult=money(booking.total_minor // adults), total=money(booking.total_minor), amount_paid=money(booking.paid_minor),
        amount_due=money(due), payment_status=booking.payment_status if booking.payment_status in ("paid", "part_paid") else "unpaid",
        can_record_payments=open_hold and due > 0 and viewer.can(RECORD_PAYMENTS),
        can_issue_tickets=open_hold and due == 0 and viewer.can(ISSUE_TICKETS) and booking.issuance_status in issuance_store.ISSUABLE,
    )
    if viewer.visibility.supplier_names and booking.provider:
        view.supplier = booking.provider
    if people is not None:
        if people.customer:
            c = people.customer
            view.customer = GroupHoldCustomer(name=f"{c.first_name} {c.last_name}".strip(), phone=f"+{c.phone_code} {c.phone}", email=c.email)
        view.travelers = [GroupHoldTraveler(
            position=t.position, title=t.title, first_name=t.first_name, last_name=t.last_name, id_type=t.id_type,
            document_number_masked=t.document_number_masked, date_of_birth_masked=t.date_of_birth_masked, nationality=t.nationality,
            document_country=t.document_country, document_expiry_masked=pii.mask_date(t.document_expiry.isoformat()) if t.document_expiry else None,
            ticket_number=t.ticket_number,
        ) for t in people.travelers]
        view.payments = [GroupHoldPayment(id=p.id, method=p.method, received_into=p.received_into, amount=money(p.amount_minor),
                                          reference=p.reference, received_at=p.received_at) for p in people.payments]
    return view


def _refused(error: store.GroupHoldRefused) -> GroupHoldError:
    return GroupHoldError(error.status, error.message, error.code)


async def read(reference: str, viewer: BackOfficeUser) -> GroupHoldView:
    now = _now()
    try:
        booking = await run_in_threadpool(store.expire_if_over, reference, now)
    except store.GroupHoldRefused as error:
        raise _refused(error) from None
    people = await run_in_threadpool(booking_people.read, reference)
    return build_view(booking, people, viewer, now)


# ---------- hold ----------

class _People(BaseModel):
    """The shape flight_holds.resolve_saved works on (contact and passengers)."""
    contact: HoldContact
    passengers: list[HoldPassenger]


def _check_adults(request: GroupHoldRequest, ticket: GroupTicket, passengers: list[HoldPassenger]) -> date | None:
    """Adults only, one per seat, 12 or over on the travel date, passport valid on the travel date."""
    travel = None
    try:
        travel = date.fromisoformat(ticket.departure_date or "")
    except ValueError:
        pass
    for index, passenger in enumerate(passengers):
        path = ["passengers", index]
        if passenger.type != "ADT":
            raise GroupHoldError(422, "Group fares are for adults only.", "adults_only", [(path + ["type"], "Adults only.")])
        if passenger.title == "MASTER":
            raise GroupHoldError(422, "Adults take the title Mr, Mrs or Miss.", "invalid_passengers", [(path + ["title"], "Mr, Mrs or Miss.")])
        if travel and passenger.date_of_birth and age_on(passenger.date_of_birth, travel) < 12:
            raise GroupHoldError(422, f"{passenger.first_name} is under 12 on the travel date. Group fares are for adults only.", "adults_only",
                                 [(path + ["date_of_birth"], "Must be 12 or over on the travel date.")])
        if travel and passenger.document_expiry and passenger.document_expiry < travel:
            raise GroupHoldError(422, f"{passenger.first_name}'s passport expires before the travel date.", "invalid_passengers",
                                 [(path + ["document_expiry"], "Expires before the travel date.")])
    return travel


def _itinerary(ticket: GroupTicket) -> list[dict]:
    codes = sector_codes(ticket.sector)
    time = _departure_time(ticket)
    departure = f"{ticket.departure_date}T{time}" if ticket.departure_date and time else ticket.departure_date
    baggage = str(ticket.baggage).strip() if ticket.baggage not in (None, "") else None
    meal = str(ticket.meal).strip() if ticket.meal not in (None, "") else None
    return [{"direction": "outbound", "origin": codes[0] if codes else (ticket.sector or "—"), "destination": codes[1] if codes else "—",
             "airline_name": ticket.airline, "departure_local": departure, "baggage": baggage, "meal": meal,
             "logo": ticket.airline_logo}]


async def hold(request: GroupHoldRequest, viewer: BackOfficeUser,
               fetch: Callable[[], Awaitable[list[GroupInventory]]] | None = None) -> GroupHoldView:
    if viewer.role not in BACK_OFFICE_ROLES:
        raise GroupHoldError(403, "Only back-office users can hold group seats.", "not_allowed")
    if any(passenger.type != "ADT" for passenger in request.passengers):
        raise GroupHoldError(422, "Group fares are for adults only.", "adults_only")
    if len(request.passengers) != request.adults:
        raise GroupHoldError(422, f"Enter {request.adults} adult{'s' if request.adults != 1 else ''}, as searched.", "passengers_mismatch")
    try:
        contact = request.contact.to_hold_contact()
    except ContactError as error:
        raise GroupHoldError(422, error.message, "invalid_customer", [(["contact", error.field], error.message)]) from None
    except ValidationError as error:
        fields = {"first_name": "full_name", "last_name": "full_name", "phone_code": "phone"}
        paths = [(["contact", fields.get(str(e["loc"][0]), str(e["loc"][0])) if e["loc"] else "full_name"], e["msg"].removeprefix("Value error, "))
                 for e in error.errors()]
        raise GroupHoldError(422, "Check the customer's details.", "invalid_customer", paths) from None
    try:
        ticket = await find_ticket(str(request.ticket_id), fetch)
    except TicketNotFound as error:
        raise GroupHoldError(error.status if error.status != 404 else 409,
                             "This group fare is no longer available. Search again." if error.status != 503
                             else "Group fares can't be checked right now. Nothing was held.", "fare_gone") from None
    provider = provider_for(ticket.vendor)
    if provider is None:
        raise GroupHoldError(409, "Seats on this group fare can't be held through Travelian yet. Nothing was held.", "hold_not_supported")
    if ticket.remaining is not None and ticket.remaining < request.adults:
        raise GroupHoldError(409, f"Only {ticket.remaining} seat{'s' if ticket.remaining != 1 else ''} left on this fare. Nothing was held.",
                             "not_enough_seats")
    if not _markup_configured():
        raise GroupHoldError(409, "No selling price is configured for group fares. Nothing was held.", "no_selling_price")

    owner = None  # back-office holds are Travelian's own (saved people are scoped by owner)
    try:
        people = _People(contact=contact, passengers=[HoldPassenger.model_validate(p.model_dump(exclude={"gender"})) for p in request.passengers])
        resolved, links = await run_in_threadpool(flight_holds.resolve_saved, people, owner)
    except flight_holds.HoldError as error:
        raise GroupHoldError(error.status, error.message, error.code, error.field_errors) from None
    _check_adults(request, ticket, resolved.passengers)
    for index, passenger in enumerate(request.passengers):
        title = resolved.passengers[index].title
        if passenger.gender and title and (passenger.gender == "M") != (title == "MR"):
            raise GroupHoldError(422, "The title and gender don't match.", "invalid_passengers",
                                 [(["passengers", index, "gender"], "Mr is male; Mrs and Miss are female.")])

    # The hold. Never retried: a failure may still have held seats, so staff check the supplier.
    try:
        held = await provider.hold(ticket, resolved.passengers, resolved.contact)
    except SupplierRefused:
        raise GroupHoldError(409, "The supplier didn't hold the seats. Nothing was held.", "not_held") from None
    except Exception:  # noqa: BLE001 - unclear outcome, never retried
        logger.warning("Group hold failed", extra={"vendor": ticket.vendor})
        raise GroupHoldError(502, "The supplier didn't confirm the hold. Check with the supplier before trying again.", "not_confirmed_hold") from None
    selling = selling_price(held.cost_per_adult)
    if selling is None or held.expires_at.tzinfo is None:
        logger.error("Group hold answer unusable", extra={"vendor": ticket.vendor, "pnr": held.pnr})
        raise GroupHoldError(502, f"The supplier held the seats (PNR {held.pnr}) but its answer can't be used. Release it with the supplier.",
                             "not_confirmed_hold", pnr=held.pnr)

    travelers = flight_holds._travelers(resolved, links)
    try:
        reference = await run_in_threadpool(lambda: supplier_bookings.save_hold(
            provider=ticket.vendor, pnr=held.pnr, supplier_ref=held.supplier_ref, hold_expires_at=held.expires_at, currency=selling.currency,
            selling_total_minor=selling.amount_minor * request.adults, supplier_total_minor=held.cost_per_adult.amount_minor * request.adults,
            supplier_response={"adults": request.adults, "ticket": {"updated_at": ticket.updated_at, "ticket_type": ticket.ticket_type}},
            actor_user_id=viewer.user_id, customer=flight_holds._customer(resolved), travelers=travelers, itinerary=_itinerary(ticket),
            audit={"pnr": held.pnr, "time_limit": held.expires_at.isoformat(), "amount_minor": selling.amount_minor * request.adults,
                   "currency": selling.currency, "pax_count": request.adults},
            product="group", source=None, reference_prefix=REFERENCE_PREFIX,
        ))
    except Exception:  # noqa: BLE001
        logger.error("Group hold created but not saved", extra={"pnr": held.pnr})
        raise GroupHoldError(500, f"The seats were held at the supplier (PNR {held.pnr}) but couldn't be saved. Record this PNR.",
                             "not_saved", pnr=held.pnr) from None
    document = None
    customer = request.contact
    if customer.document_number and customer.id_type:
        nationality = "PAK" if customer.id_type == "NATI" else next(
            (p.document_country for p in resolved.passengers if p.document_number == customer.document_number and p.document_country), None)
        if nationality:
            document = saved_people.DocumentInput(customer.id_type, nationality, customer.document_number)
    outcomes = await run_in_threadpool(flight_holds._save_people, resolved, reference, viewer.user_id, owner, links, document)
    view = await read(reference, viewer)
    view.saved_people = outcomes
    return view


# ---------- price check, release, issue ----------

def _held_ref(booking: store.GroupBooking) -> HeldRef:
    return HeldRef(booking.pnr, booking.supplier_ref or "", booking.adults)


async def _open_hold(reference: str) -> tuple[store.GroupBooking, GroupHoldProvider]:
    try:
        booking = await run_in_threadpool(store.expire_if_over, reference, _now())
    except store.GroupHoldRefused as error:
        raise _refused(error) from None
    status = _status(booking, _now())
    if status == "expired":
        raise GroupHoldError(409, "The hold has expired. Search again and hold new seats.", "hold_expired")
    if status != "held":
        raise GroupHoldError(409, "This booking isn't on hold.", "not_held")
    provider = provider_for(booking.provider)
    if provider is None or not booking.supplier_ref:
        raise GroupHoldError(409, "This hold can't be managed through Travelian. Check with the supplier.", "hold_not_supported")
    return booking, provider


async def _new_price(booking: store.GroupBooking, provider: GroupHoldProvider) -> tuple[GroupMoney, GroupMoney, int]:
    """(new selling per adult, supplier cost per adult, new total). Read-only at the supplier."""
    try:
        cost = await provider.price(_held_ref(booking))
    except Exception:  # noqa: BLE001
        raise GroupHoldError(502, "The price couldn't be checked with the supplier. Nothing was changed.", "price_check_failed") from None
    selling = selling_price(cost)
    if selling is None or selling.currency != booking.currency:
        raise GroupHoldError(409, "No selling price could be worked out for the new supplier price. Nothing was changed.", "no_selling_price")
    return selling, cost, selling.amount_minor * booking.adults


def _check(booking: store.GroupBooking, selling: GroupMoney, total: int) -> GroupPriceCheck:
    money = lambda minor: GroupMoney(amount_minor=minor, currency=booking.currency)  # noqa: E731
    return GroupPriceCheck(changed=total != booking.total_minor, held_total=money(booking.total_minor), new_total=money(total),
                           difference=money(total - booking.total_minor), new_price_per_adult=selling)


async def price_check(reference: str, viewer: BackOfficeUser) -> GroupPriceCheck:
    booking, provider = await _open_hold(reference)
    selling, _, total = await _new_price(booking, provider)
    return _check(booking, selling, total)


async def release(reference: str, viewer: BackOfficeUser) -> GroupHoldView:
    booking, provider = await _open_hold(reference)
    if booking.issuance_status not in ("not_issued", "requested"):
        raise GroupHoldError(409, "Tickets are being issued for this booking. It can't be released.", "issuing")
    try:
        await provider.release(_held_ref(booking))
    except Exception:  # noqa: BLE001 - never retried; nothing changes here
        logger.warning("Group release failed", extra={"pnr": booking.pnr})
        raise GroupHoldError(502, "The supplier didn't confirm the release. The seats may still be held: check with the supplier.",
                             "not_released") from None
    try:
        await run_in_threadpool(store.mark_released, reference, viewer.user_id)
    except store.GroupHoldRefused as error:
        raise _refused(error) from None
    return await read(reference, viewer)


async def issue(reference: str, viewer: BackOfficeUser, accepted_total: GroupMoney | None = None) -> GroupIssueResult:
    if not viewer.can(ISSUE_TICKETS):
        raise GroupHoldError(403, "Issuing tickets requires the issue tickets permission.", "not_allowed")
    booking, provider = await _open_hold(reference)
    if booking.paid_minor < booking.total_minor:
        due = booking.total_minor - booking.paid_minor
        raise GroupHoldError(409, f"Record the full payment first: {money_text(due, booking.currency)} is still due. Nothing was issued.", "payment_due")

    # 1. Check the price again (read-only). A different price needs the customer's agreement.
    selling, cost, total = await _new_price(booking, provider)
    if total != booking.total_minor:
        check = _check(booking, selling, total)
        agreed = accepted_total is not None and accepted_total.currency == booking.currency and accepted_total.amount_minor == total
        if not agreed:
            raise GroupHoldError(409, f"The price has changed to {money_text(total, booking.currency)}. Nothing was issued.", "price_changed",
                                 held_total=check.held_total.model_dump(), new_total=check.new_total.model_dump(),
                                 difference=check.difference.model_dump())
        try:
            booking = await run_in_threadpool(store.accept_price, reference, viewer.user_id, booking.total_minor, total,
                                              cost.amount_minor * booking.adults)
        except store.GroupHoldRefused as error:
            raise _refused(error) from None
        if booking.paid_minor < booking.total_minor:
            due = booking.total_minor - booking.paid_minor
            raise GroupHoldError(409, f"The new total is {money_text(total, booking.currency)}. Record the remaining "
                                      f"{money_text(due, booking.currency)}, then issue the tickets.", "payment_due")

    # 2. Claim (a second click is refused), then ONE issue call, never retried.
    try:
        target = await run_in_threadpool(issuance_store.claim, reference, viewer.user_id, _now(),
                                         settings.ticketing_deadline_margin_seconds, ("group",))
    except issuance_store.IssuanceRefused as error:
        raise GroupHoldError(error.status, error.message, "not_issuable") from None
    previous = target.issuance_status
    try:
        numbers = await provider.issue(_held_ref(booking), cost)
    except SupplierRefused:
        await run_in_threadpool(issuance_store.release, target, viewer.user_id, previous, "issuance_failed", {"supplier_answer": "refused"})
        return GroupIssueResult(result="not_issued", message="The supplier didn't issue the tickets. You can try again.",
                                hold=await read(reference, viewer))
    except Exception:  # noqa: BLE001 - outcome unclear: never retried
        numbers = None
    positions = [position for position, _ in target.traveler_types]
    if numbers and len(numbers) == len(positions) and all(numbers):
        await run_in_threadpool(issuance_store.mark_issued, target, viewer.user_id, dict(zip(positions, numbers)), "issuance_issued",
                                {"pnr_status": "issued"})
        return GroupIssueResult(result="issued", message="Tickets issued.", hold=await read(reference, viewer))
    await run_in_threadpool(issuance_store.release, target, viewer.user_id, "unknown", "issuance_unknown",
                            {"reason_code": "unclear_after_ticketing"})
    return GroupIssueResult(result="unknown", message="The tickets' status is unknown. Don't issue again: check with the supplier.",
                            hold=await read(reference, viewer))


__all__ = ["GroupHoldError", "GroupHoldProvider", "HeldRef", "HeldSeats", "PROVIDERS", "SUPPORT", "SupplierRefused",
           "hold", "issue", "price_check", "provider_for", "read", "release"]
