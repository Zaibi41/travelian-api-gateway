"""Group Ticketing hold lifecycle (admin, adults only): hold seats -> price shown -> payments ->
price re-checked -> issue tickets; or release; or the hold expires.

Each group source plugs in as a GroupHoldProvider (docs/api/group-holds.md). Skypass holds and
releases seats (no issuing: its submit-booking is not built). Dhillu & Sahi holds through add-booking
(GROUP_DHILLU_SAHI_HOLD_ENABLED); it has no release call, so nothing is released at our expiry (the
supplier cancels the hold by itself). State-changing supplier calls are never retried here; the expiry
job (group_hold_expiry) retries only releases.

Order: our record is written first ('hold_pending'), then the supplier is called, then the record is
completed ('held'). A supplier failure marks it 'hold_failed' (nothing pending). If the supplier held
but the record can't be completed, the supplier PNR is kept on a 'hold_unrecorded' record and the
seats are released where the provider can.

The hold time is Travelian's own: expiry = creation + GROUP_HOLD_MINUTES, kept on the booking.
No supplier expiry is used. After it, payments and issuing are refused even if the supplier still
holds the seats, and the job gives the seats back. The supplier price per adult from the listing
becomes the selling price (group markup) and the booking total. Before issuing, the supplier cost is
checked again. The selling price is never recalculated: when the cost moved, issuing stops with
"supplier_cost_changed" until the issuer confirms the new cost (accepted_supplier_cost), and with
"below_cost" too when it is above the selling price (accept_below_cost); or the hold is released. Supplier names, supplier PNRs and cost never leave this
module except for viewers allowed to see them.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.field_visibility import BACK_OFFICE_ROLES, ISSUE_TICKETS, RECORD_PAYMENTS, BackOfficeUser
from app.db import booking_people, group_holds as store
from app.db import issuance as issuance_store
from app.schemas.group_holds import (
    ContactError, FareTermsView, GroupHeldPriceChange, GroupHoldCustomer, GroupHoldFlight, GroupHoldPayment, GroupHoldRequest, GroupHoldTraveler, GroupHoldView,
    GroupIssueResult, GroupPriceCheck, age_on,
)
from app.schemas.group_ticketing import GroupInventory, GroupSource, GroupTicket
from app.schemas.live_flights import HoldContact, HoldPassenger
from app.services import flight_holds, group_terms, notifications, pricing
from app.services.group_ticketing_status import ticketing_status
from app.services.group_ticketing import TicketNotFound, find_ticket, sector_codes, stock_label
from app.services.group_ticketing_view import GroupLegView, GroupMoney, _departure_time, baggage_text, selling_price, supplier_cost
from app.services.live_flights import money_text
from app.services import supplier_logos

#: The public website acting for a guest (no staff user; app.services.public_booking).
WEBSITE_ROLE = "website"

logger = logging.getLogger(__name__)
REFERENCE_PREFIX = "TRV-GRP-"


# ---------- providers ----------

@dataclass(frozen=True)
class HeldSeats:
    """What a provider returns for a confirmed hold. The expiry is Travelian's own (GROUP_HOLD_MINUTES)."""
    pnr: str | None
    supplier_ref: str          # what the provider needs to release or price the hold; stays in the gateway
    cost_per_adult: GroupMoney  # supplier price per adult
    supplier_answer: dict | None = None  # what the supplier answered (kept on our record; never returned)
    supplier_total: GroupMoney | None = None  # the booking total the supplier held at, when it says (all travelers)
    booked_at: datetime | None = None  # when the supplier booking was made (our clock, right after add-booking)
    supplier_expires_at: datetime | None = None  # the supplier's own expiry, already checked as usable (Dhillu & Sahi cancel_at)

    def __repr__(self) -> str:
        return f"HeldSeats(pnr={self.pnr})"


@dataclass(frozen=True)
class HeldRef:
    pnr: str | None
    supplier_ref: str
    adults: int
    cost_per_adult: GroupMoney | None = None  # the supplier price per adult the booking holds at


class SupplierRefused(Exception):
    """The supplier answered clearly that it did nothing (no hold, nothing released, nothing issued)."""


class PriceOrSeatsChanged(SupplierRefused):
    """The supplier's availability check said the price or the seats changed (nothing was done): search again."""


class GroupHoldProvider(Protocol):
    supports_issue: bool
    issue_unavailable_reason: str

    async def hold(self, ticket: GroupTicket, passengers: list[HoldPassenger], contact: HoldContact) -> HeldSeats: ...
    async def price(self, held: HeldRef) -> GroupMoney: ...  # the supplier cost for ALL the held seats (not per adult)
    async def release(self, held: HeldRef) -> None: ...
    async def issue(self, held: HeldRef, cost_per_adult: GroupMoney) -> list[str]: ...


def _dhillu_sahi() -> GroupHoldProvider | None:
    from app.vendors.group_ticketing.dhillu_sahi.holds import DhilluSahiHoldProvider, is_configured

    return DhilluSahiHoldProvider() if settings.group_dhillu_sahi_hold_enabled and is_configured() else None


def _al_haider() -> GroupHoldProvider | None:
    from app.vendors.group_ticketing.al_haider.holds import AlHaiderHoldProvider, is_configured

    return AlHaiderHoldProvider() if settings.group_al_haider_hold_enabled and is_configured() else None


def _skypass() -> GroupHoldProvider | None:
    from app.vendors.group_ticketing.skypass.holds import SkypassHoldProvider
    from app.vendors.group_ticketing.skypass.inventory import is_configured

    return SkypassHoldProvider() if is_configured() else None


#: Group source (GroupTicket.vendor) -> provider factory (None when its credentials are missing).
#: Add a source only with its supplier's documented hold and release contracts (gateway AGENTS.md).
PROVIDERS: dict[str, Callable[[], GroupHoldProvider | None]] = {"skypass": _skypass, "dhillu_sahi": _dhillu_sahi, "al_haider": _al_haider}
#: Sources with no release or cancel call: nothing is sent to the supplier when our hold ends (it ends there by itself).
NO_RELEASE_SOURCES = {"dhillu_sahi", "al_haider"}

#: What each group source supports today (reported, not used for routing).
SUPPORT = {
    "skypass": "Inventory; hold and release (hold-available-seats, release-seats; no hold id, expiry or price in the answer); "
               "price re-check from the ticket detail. No issuing: submit-booking is not built.",
    "dhillu_sahi": "Inventory; hold through add-booking (holds unpaid, cancelled by itself at the supplier; no cancel or "
                   "release call) behind GROUP_DHILLU_SAHI_HOLD_ENABLED; booking detail read after the hold. No price re-check, no issuing.",
    "al_haider": "Inventory; hold through create/booking behind GROUP_AL_HAIDER_HOLD_ENABLED (default off until Al Haider confirms "
                 "that an unpaid booking expires). No release (its cancel call is permission based and unused), no price re-check, no issuing.",
}

NO_ISSUE_REASON = "Issuing is not available for this provider yet"


def provider_for(vendor: str | None) -> GroupHoldProvider | None:
    factory = PROVIDERS.get(vendor or "")
    return factory() if factory else None


def supports_issue(provider: GroupHoldProvider | None) -> bool:
    return bool(provider is not None and getattr(provider, "supports_issue", False))


def hold_minutes(provider: GroupHoldProvider | None = None) -> int:
    """The provider's own hold time when it has one (Dhillu & Sahi: DHILLU_SAHI_HOLD_DEFAULT_MINUTES), else GROUP_HOLD_MINUTES."""
    return getattr(provider, "hold_minutes", None) or settings.group_hold_minutes


def hold_expiry(created_at: datetime, provider: GroupHoldProvider | None = None) -> datetime:
    """Travelian's own hold time: creation + hold_minutes(provider). Never a supplier value."""
    return created_at + timedelta(minutes=hold_minutes(provider))


def hold_time_note(booking: store.GroupBooking) -> str | None:
    """For viewers with supplier access, under the countdown: where the hold time came from."""
    hold_time = booking.hold_time
    if hold_time.get("source") == "default":
        return f"Hold time estimated ({hold_time.get('minutes')} min default)"
    if hold_time.get("source") == "supplier":
        return "Hold time from supplier"
    return None


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


# ---------- hold failures ----------

def error_reason(error: Exception) -> str:
    """What went wrong, without data: 'IntegrityError/CheckViolation (ck_bookings_pnr)'."""
    original = getattr(error, "orig", None)
    reason = f"{type(error).__name__}/{type(original).__name__}" if original is not None else type(error).__name__
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    return f"{reason} ({constraint})" if constraint else reason


def _for_viewer(viewer: BackOfficeUser, full: str, short: str) -> str:
    """The real reason for viewers with supplier access; a short sentence for everyone else."""
    return full if viewer.visibility.supplier_names else short


async def _mark_failed(booking_id: str, viewer: BackOfficeUser, reason_code: str) -> None:
    try:
        await run_in_threadpool(store.mark_hold_failed, booking_id, viewer.user_id, reason_code)
    except Exception as error:  # noqa: BLE001 - the supplier holds nothing; a stale pending row is only noise
        logger.error("Group hold not marked failed", extra={"booking_id": booking_id, "error_type": error_reason(error)})


async def _research(ticket_id: str | None, adults: int, old_cost: GroupMoney | None,
                    fetch: Callable[[], Awaitable[list[GroupInventory]]] | None, nothing: str) -> GroupHoldError | None:
    """Search again after the supplier refused or said the price or seats changed, and answer with the same messages
    as before a hold: gone, not enough seats, or the new price. None when none of them applies."""
    if not ticket_id:
        return None
    try:
        fresh = await find_ticket(ticket_id, fetch)
    except TicketNotFound as error:
        return None if error.status == 503 else GroupHoldError(409, f"This group fare is no longer available. Search again. {nothing}", "fare_gone")
    if fresh.remaining is not None and fresh.remaining < adults:
        return GroupHoldError(409, f"Only {fresh.remaining} seat{'s' if fresh.remaining != 1 else ''} left on this fare. {nothing}", "not_enough_seats")
    old, new = selling_price(old_cost), selling_price(supplier_cost(fresh))
    if new is not None and old is not None and new != old:
        return GroupHoldError(409, f"The price has changed to {money_text(new.amount_minor, new.currency)} per adult. {nothing} "
                                   "Search again to hold at the new price.", "price_changed",
                              held_total=old.model_copy(update={"amount_minor": old.amount_minor * adults}).model_dump(),
                              new_total=new.model_copy(update={"amount_minor": new.amount_minor * adults}).model_dump(),
                              difference=GroupMoney(amount_minor=(new.amount_minor - old.amount_minor) * adults, currency=new.currency).model_dump())
    return None


async def _after_refusal(request: GroupHoldRequest, ticket: GroupTicket,
                         fetch: Callable[[], Awaitable[list[GroupInventory]]] | None) -> GroupHoldError:
    """The supplier refused (e.g. the ticket changed since the listing, or sold out): search again."""
    return await _research(str(request.ticket_id), request.adults, supplier_cost(ticket), fetch, "Nothing was held.") \
        or GroupHoldError(409, "The supplier didn't hold the seats. Nothing was held.", "not_held")


#: Shown to viewers with supplier access on a hold the supplier made but Travelian couldn't record.
NOT_RECORDED_EXPIRES = "Supplier hold not recorded. It will cancel by itself at the supplier shortly. Record the PNR and check with the supplier."
NOT_RELEASED = "Supplier hold not released, release manually"
NOT_RECORDED_RELEASING = "Supplier hold not recorded, the seats are being released"
#: Sources without a release call whose supplier expiry isn't confirmed: the unrecorded hold must be released by hand.
EXPIRY_UNCONFIRMED_SOURCES = {"al_haider"}


def _unreleasable_notice(vendor: str | None) -> str:
    return NOT_RELEASED if vendor in EXPIRY_UNCONFIRMED_SOURCES else NOT_RECORDED_EXPIRES


async def _unrecorded(booking_id: str, reference: str, held: HeldSeats, provider: GroupHoldProvider, vendor: str, adults: int,
                      viewer: BackOfficeUser, reason: str) -> GroupHoldError:
    """The supplier held seats but our record couldn't be completed. Keep the supplier PNR on a 'hold_unrecorded'
    record (or, if even that fails, in the error log), then release the seats where the provider can (one call
    now; the expiry job retries up to GROUP_HOLD_RELEASE_ATTEMPTS). Returns the error to answer with."""
    now = _now()
    releasable = bool(getattr(provider, "can_release", True))
    logger.error("Group hold made at the supplier but not recorded",
                 extra={"vendor": vendor, "reference": reference, "pnr": held.pnr, "error_type": reason})
    kept = True
    try:
        await run_in_threadpool(lambda: store.mark_unrecorded(booking_id, viewer.user_id, supplier_pnr=held.pnr, supplier_ref=held.supplier_ref,
                                                              error=reason, now=now, release_pending=releasable))
    except Exception as error:  # noqa: BLE001
        kept = False
        logger.critical("Supplier hold PNR not kept on any record: release or note it now",
                        extra={"vendor": vendor, "reference": reference, "pnr": held.pnr, "error_type": error_reason(error)})
    outcome = _unreleasable_notice(vendor).rstrip(".") + "."
    if releasable:
        released, code = True, None
        try:
            await provider.release(HeldRef(held.pnr, held.supplier_ref, adults))
        except Exception as error:  # noqa: BLE001 - recorded; the expiry job retries
            released, code = False, getattr(error, "error_code", type(error).__name__)
        logger.warning("Release of an unrecorded supplier hold", extra={"reference": reference, "pnr": held.pnr, "released": released, "error_code": code})
        if kept:
            try:
                await run_in_threadpool(store.mark_release_attempt, reference, viewer.user_id, released, code, now, settings.group_hold_release_attempts)
                if released:
                    await run_in_threadpool(store.mark_unrecorded_released, reference, viewer.user_id)
            except Exception as error:  # noqa: BLE001
                logger.error("Release outcome not recorded", extra={"reference": reference, "error_type": error_reason(error)})
        outcome = "The seats were released at the supplier." if released else (
            "Releasing the seats at the supplier failed; Travelian will keep trying. " + NOT_RELEASED + " if it keeps failing.")
    pnr = f" (PNR {held.pnr})" if held.pnr else ""
    where = f" It is kept on booking {reference}." if kept else " It could not be kept on any booking: note the PNR now."
    full = f"The seats were held at the supplier{pnr} but the booking couldn't be saved ({reason}). {outcome}{where}"
    extra = {"booking_id": reference, **({"pnr": held.pnr} if held.pnr else {})} if viewer.visibility.supplier_names else {}
    return GroupHoldError(500, _for_viewer(viewer, full, "The booking couldn't be saved. Ask an admin to check it before trying again."),
                          "not_saved", **extra)


# ---------- read ----------

def _status(booking: store.GroupBooking, now: datetime) -> str:
    if booking.booking_status == store.HOLD_UNRECORDED:
        return "not_recorded"
    if booking.booking_status == "requested":  # a website request (live booking off): nothing held at the supplier
        return "requested"
    if booking.issuance_status in ("processing", "unknown"):
        return booking.issuance_status
    if booking.booking_status == "ticket_issued" or booking.issuance_status == "issued":
        return "issued"
    if booking.booking_status == "confirmed":
        return "confirmed"
    if booking.booking_status == "cancelled":
        return "released"
    if booking.booking_status == "expired" or (booking.booking_status == "held" and store.is_over(booking, now)):
        return "expired"
    return "held"


def _leg_view(segment: dict) -> GroupLegView:
    departure, arrival = str(segment.get("departure_local") or ""), str(segment.get("arrival_local") or "")
    return GroupLegView(direction="return" if segment.get("direction") == "return" else "outbound", flight_number=segment.get("flight_number"),
                        origin=segment.get("origin"), destination=segment.get("destination"),
                        departure_date=departure[:10] or None, departure_time=departure[11:16] or None,
                        arrival_date=arrival[:10] or None, arrival_time=arrival[11:16] or None)


def _flight(booking: store.GroupBooking) -> GroupHoldFlight:
    segment = booking.itinerary[0] if booking.itinerary else {}
    departure = str(segment.get("departure_local") or "")
    return GroupHoldFlight(
        airline=segment.get("airline_name"), origin=segment.get("origin"), destination=segment.get("destination"),
        sector=f"{segment['origin']} - {segment['destination']}" if segment.get("origin") and segment.get("destination") else None,
        departure_date=departure[:10] or None, departure_time=departure[11:16] or None,
        logo_url=supplier_logos.register(segment.get("logo"), supplier_logos.airline_label(segment.get("airline_name"), segment.get("flight_number")))
        if segment.get("logo") else None, baggage=segment.get("baggage"),
        legs=[_leg_view(item) for item in booking.itinerary if isinstance(item, dict)],
    )


def supplier_notice(booking: store.GroupBooking, provider: GroupHoldProvider | None, status: str) -> str | None:
    """For viewers with supplier access: what happens to the seats at the supplier."""
    if status == "not_recorded":
        if booking.release.get("status") == store.RELEASE_PENDING and not booking.release_failed:
            return NOT_RECORDED_RELEASING
        return NOT_RELEASED if booking.release_failed else _unreleasable_notice(booking.provider)
    if booking.supplier_cancelled:
        return SUPPLIER_CANCELLED
    return getattr(provider, "supplier_hold_notice", None) if status in ("held", "expired") else None


#: For viewers with supplier access when the supplier cancelled the booking (Dhillu & Sahi cancel_status '1').
SUPPLIER_CANCELLED = "Cancelled at the supplier"


async def refresh_supplier(booking: store.GroupBooking) -> store.GroupBooking:
    """Read the supplier's booking again where the provider can (Dhillu & Sahi agent-booking-detail) for an open hold:
    keeps its amounts and, when it says cancelled, marks ours expired. A failed read changes nothing."""
    if booking.booking_status != "held" or not booking.supplier_ref:
        return booking
    provider = provider_for(booking.provider)
    refresh = getattr(provider, "refresh", None)
    if refresh is None:
        return booking
    try:
        booking_amounts = await refresh(_held_ref(booking))
    except Exception as error:  # noqa: BLE001 - informational; the hold stands
        logger.warning("Supplier booking not read", extra={"reference": booking.reference, "error_code": getattr(error, "error_code", type(error).__name__)})
        return booking
    cancelled = str(booking_amounts.get("cancel_status", "")).strip() == "1"
    if cancelled:
        logger.warning("Supplier cancelled a held booking", extra={"reference": booking.reference})
    # Still on the default hold time: switch to the supplier's expiry as soon as a read gives a usable one.
    new_expiry = None
    expiry_from = getattr(provider, "expiry_from", None)
    if not cancelled and expiry_from is not None and booking.hold_time.get("source") == "default":
        try:
            booked_at = datetime.fromisoformat(str(booking.hold_time.get("from")))
        except ValueError:
            booked_at = None
        if booked_at is not None:
            new_expiry = expiry_from(booking_amounts, booked_at, booking.supplier_ref)
    return await run_in_threadpool(store.save_supplier_amounts, booking.reference, booking_amounts, cancelled, _now(), new_expiry)


async def refresh_for_payment(reference: str) -> None:
    """Before a payment on a group booking: read the supplier again, so a supplier cancellation blocks the payment."""
    try:
        booking = await run_in_threadpool(store.load, reference)
        await refresh_supplier(booking)
    except store.GroupHoldRefused:
        return  # not a group booking
    except Exception as error:  # noqa: BLE001 - best effort; the payment's own checks still run
        logger.warning("Supplier booking not read before a payment", extra={"reference": reference, "error_type": error_reason(error)})


async def accept_held_price(reference: str, viewer: BackOfficeUser) -> GroupHoldView:
    """The customer agreed to the price the supplier held at."""
    if not viewer.can(RECORD_PAYMENTS):
        raise GroupHoldError(403, "Confirming the new price requires the record payments permission.", "not_allowed")
    try:
        await run_in_threadpool(store.accept_held_price, reference, viewer.user_id)
    except store.GroupHoldRefused as error:
        raise _refused(error) from None
    return await read(reference, viewer)


def booking_source(booking: store.GroupBooking) -> GroupSource | None:
    """The identity kept on the booking; the booking's own provider and PNR for holds made before it was kept."""
    source = booking.source
    return GroupSource.of(source.get("provider") or booking.provider, source.get("supplier_pnr") or booking.pnr,
                          source.get("supplier_group_id"), source.get("supplier_ticket_id"))


def source_record(ticket: GroupTicket, held_pnr: str | None) -> dict[str, str | None]:
    """What the booking keeps of the listing row: provider, supplier PNR (the hold's, else the listing's), group and ticket IDs."""
    return {"provider": ticket.vendor, "supplier_pnr": held_pnr or ticket.pnr,
            "supplier_group_id": ticket.supplier_group_id, "supplier_ticket_id": ticket.supplier_ticket_id}


def fare_terms_view(stored: group_terms.Acknowledgement | None) -> FareTermsView | None:
    return None if stored is None else FareTermsView(text=stored.text, version=stored.version, acknowledged_by=stored.acknowledged_by,
                                                     acknowledged_by_user_id=stored.actor_user_id, acknowledged_at=stored.at)


def build_view(booking: store.GroupBooking, people: booking_people.People | None, viewer: BackOfficeUser, now: datetime) -> GroupHoldView:
    money = lambda minor: GroupMoney(amount_minor=minor, currency=booking.currency)  # noqa: E731
    status = _status(booking, now)
    due = max(booking.total_minor - booking.paid_minor, 0)
    adults = max(booking.adults, 1)
    open_hold = status == "held"
    provider = provider_for(booking.provider)
    issuable = supports_issue(provider)
    view = GroupHoldView(
        booking_id=booking.reference, status=status, hold_expires_at=booking.hold_expires_at, adults=booking.adults,
        flight=_flight(booking), stock=stock_label(booking.provider or ""),
        price_per_adult=money(booking.total_minor // adults), total=money(booking.total_minor), amount_paid=money(booking.paid_minor),
        amount_due=money(due), payment_status=booking.payment_status if booking.payment_status in ("paid", "part_paid") else "unpaid",
        can_record_payments=open_hold and due > 0 and viewer.can(RECORD_PAYMENTS) and not booking.price_change_pending,
        can_issue_tickets=issuable and open_hold and due == 0 and viewer.can(ISSUE_TICKETS) and booking.issuance_status in issuance_store.ISSUABLE,
        issue_unavailable_reason=None if issuable else NO_ISSUE_REASON,
        can_release=open_hold and provider is not None and booking.provider not in NO_RELEASE_SOURCES,
    )
    if booking.price_change_pending and open_hold:
        held_cost = booking.supplier_cost_minor if booking.supplier_cost_minor is not None else int(booking.price_change.get("held_cost_total_minor") or 0)
        view.price_change = GroupHeldPriceChange(selling_total=money(booking.total_minor), below_cost=booking.total_minor < held_cost)
        if viewer.visibility.cost_and_margin:
            shown = int(booking.price_change.get("shown_cost_total_minor") or 0)
            view.price_change.shown_cost, view.price_change.held_cost = money(shown), money(held_cost)
            view.price_change.difference, view.price_change.profit = money(held_cost - shown), money(booking.total_minor - held_cost)
    if viewer.visibility.cost_and_margin and booking.supplier_cost_minor is not None:
        view.supplier_cost = money(booking.supplier_cost_minor)
        view.profit = money(booking.total_minor - booking.supplier_cost_minor)
    view.can_set_selling_price = open_hold and booking.paid_minor == 0 and viewer.can(RECORD_PAYMENTS)
    view.final_pnr = booking.final_pnr
    view.ticketing_status = ticketing_status(booking.booking_status, booking.issuance_status, booking.final_pnr, due == 0)
    view.can_edit_tickets = (viewer.can(ISSUE_TICKETS) and due == 0 and booking.booking_status in store.TICKETABLE
                             and not (status == "held" and store.is_over(booking, now)))
    view.itinerary_available = view.ticketing_status == "ticketed"
    if viewer.visibility.supplier_names:
        # Supplier name and the supplier's PNR: only with the supplier names permission.
        view.supplier = booking.provider or None
        view.pnr = booking.pnr
        view.supplier_release_failed = booking.release_failed
        view.group_source = booking_source(booking)
        view.supplier_notice = supplier_notice(booking, provider, status)
        view.hold_time_note = hold_time_note(booking) if status in ("held", "expired") else None
    if people is not None:
        view.fare_terms = fare_terms_view(group_terms.acknowledgement(people.events))
        if people.customer:
            c = people.customer
            view.customer = GroupHoldCustomer(name=f"{c.first_name} {c.last_name}".strip(), phone=f"+{c.phone_code} {c.phone}", email=c.email)
        view.travelers = [GroupHoldTraveler(
            position=t.position, title=t.title, first_name=t.first_name, last_name=t.last_name, id_type=t.id_type,
            document_number=t.document_number, date_of_birth=t.date_of_birth, nationality=t.nationality,
            document_country=t.document_country, document_expiry=t.document_expiry,
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
        booking = await run_in_threadpool(store.expire_if_over, reference, now, frozenset(NO_RELEASE_SOURCES))
    except store.GroupHoldRefused as error:
        raise _refused(error) from None
    if booking.booking_status == store.HOLD_UNRECORDED and not viewer.visibility.supplier_names:
        raise GroupHoldError(404, "Group booking not found.", "not_found")
    booking = await refresh_supplier(booking)
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
    """The flights as held, outbound then return (the booking's Itinerary tab and the held page). The first segment
    also carries the ticket's departure time, airline and logo as before."""
    codes = sector_codes(ticket.sector)
    time = _departure_time(ticket)
    departure = f"{ticket.departure_date}T{time}" if ticket.departure_date and time else ticket.departure_date
    baggage = baggage_text(ticket.baggage, ticket.hand_carry)
    meal = str(ticket.meal).strip() if ticket.meal not in (None, "") else None
    first = {"direction": "outbound", "origin": codes[0] if codes else (ticket.sector or "—"), "destination": codes[1] if codes else "—",
             "airline_name": ticket.airline, "departure_local": departure, "baggage": baggage, "meal": meal, "logo": ticket.airline_logo}
    if not ticket.legs:
        return [first]
    segments = []
    for index, leg in enumerate(ticket.legs):
        when = lambda day, clock: f"{day}T{clock}" if day and clock else day  # noqa: E731
        segment = {"direction": "return" if leg.direction == "return" else "outbound", "origin": leg.origin or "—", "destination": leg.destination or "—",
                   "airline_name": ticket.airline, "flight_number": leg.flight_number,
                   "departure_local": when(leg.departure_date or (ticket.departure_date if index == 0 else None), leg.departure_time),
                   "arrival_local": when(leg.arrival_date or leg.departure_date, leg.arrival_time),
                   "baggage": baggage_text(leg.baggage, leg.hand_carry) or (baggage if index == 0 else None),
                   "meal": str(leg.meal).strip() if leg.meal not in (None, "") else meal}
        segments.append(segment)
    segments[0] = {**segments[0], "origin": first["origin"] if segments[0]["origin"] == "—" else segments[0]["origin"],
                   "departure_local": segments[0]["departure_local"] or departure, "logo": ticket.airline_logo}
    return segments


async def hold(request: GroupHoldRequest, viewer: BackOfficeUser,
               fetch: Callable[[], Awaitable[list[GroupInventory]]] | None = None, request_only: bool = False) -> GroupHoldView:
    """Hold seats at the supplier. request_only (website, live booking off): the same checks and record, saved as
    'requested', and the supplier is never called."""
    website = viewer.role == WEBSITE_ROLE  # a guest on the public site (app.services.public_booking)
    if viewer.role not in BACK_OFFICE_ROLES and not website:
        raise GroupHoldError(403, "Only back-office users can hold group seats.", "not_allowed")
    if not request.fare_terms_acknowledged:
        raise GroupHoldError(422, group_terms.REQUIRED, "fare_terms_not_acknowledged", [(["fare_terms_acknowledged"], group_terms.REQUIRED)])
    if any(passenger.type != "ADT" for passenger in request.passengers):
        raise GroupHoldError(422, "Group fares are for adults only.", "adults_only")
    if len(request.passengers) != request.adults:
        raise GroupHoldError(422, f"Enter {request.adults} adult{'s' if request.adults != 1 else ''}, as searched.", "passengers_mismatch")
    try:
        contact = request.contact.to_hold_contact()
    except ContactError as error:
        raise GroupHoldError(422, error.message, "invalid_customer", [(["contact", error.field], error.message)]) from None
    except ValidationError as error:
        fields = {"phone_code": "phone"}  # the card's own field names (given name, surname, phone, email)
        paths = [(["contact", fields.get(str(e["loc"][0]), str(e["loc"][0])) if e["loc"] else "first_name"], e["msg"].removeprefix("Value error, "))
                 for e in error.errors()]
        raise GroupHoldError(422, "Check the customer's details.", "invalid_customer", paths) from None
    try:
        ticket = await find_ticket(str(request.ticket_id), fetch)
    except TicketNotFound as error:
        raise GroupHoldError(error.status if error.status != 404 else 409,
                             "This group fare is no longer available. Search again." if error.status != 503
                             else "Group fares can't be checked right now. Nothing was held.", "fare_gone") from None
    provider = provider_for(ticket.vendor)
    if provider is None and not request_only:
        raise GroupHoldError(409, "Seats on this group fare can't be held through Travelian yet. Nothing was held.", "hold_not_supported")
    if ticket.remaining is not None and ticket.remaining < request.adults:
        raise GroupHoldError(409, f"Only {ticket.remaining} seat{'s' if ticket.remaining != 1 else ''} left on this fare. Nothing was held.",
                             "not_enough_seats")

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

    missing = getattr(provider, "check_passengers", lambda passengers: [])(resolved.passengers)
    if missing:
        raise GroupHoldError(422, "This fare needs a passport for every traveler: number and expiry date.", "invalid_passengers",
                             [(["passengers", index, field], "A passport is needed for this fare.") for index, field in missing])

    # The selling price per adult: the staff's (whole rupees, not below the supplier cost) or the default
    # (supplier price + the group markup; markup 0 when unset). Shared rule: app.services.pricing.
    listing_cost = supplier_cost(ticket)
    listing_selling = selling_price(listing_cost)
    if listing_cost is None or listing_selling is None:
        raise GroupHoldError(409, "The provider sent no price for this fare. Nothing was held.", "no_price")
    selling_each = _selling_each(request, listing_cost, listing_selling, viewer)
    currency = listing_cost.currency

    # 1. Our record first ('hold_pending'), with the customer and travelers. Nothing is sent to the supplier
    #    unless it is written.
    travelers = [replace(traveler, selling_minor=selling_each, cost_minor=listing_cost.amount_minor)
                 for traveler in flight_holds._travelers(resolved, links)]
    try:
        booking_id, reference = await run_in_threadpool(lambda: store.create_pending(
            provider=ticket.vendor, currency=currency,
            selling_total_minor=selling_each * request.adults,
            supplier_total_minor=listing_cost.amount_minor * request.adults,
            supplier_response={"adults": request.adults, "hold_minutes": hold_minutes(provider) if provider else None,
                               "ticket": {"id": str(request.ticket_id), "updated_at": ticket.updated_at, "ticket_type": ticket.ticket_type},
                               "source": source_record(ticket, None)},
            actor_user_id=viewer.user_id, customer=flight_holds._customer(resolved), travelers=travelers, itinerary=_itinerary(ticket),
            reference_prefix=REFERENCE_PREFIX, fare_terms=group_terms.event_details(), channel="website" if website else "staff",
            status="requested" if request_only else None,
        ))
    except Exception as error:  # noqa: BLE001 - nothing was sent to the supplier
        reason = error_reason(error)
        logger.error("Group hold record not written; supplier not called", extra={"vendor": ticket.vendor, "error_type": reason})
        raise GroupHoldError(500, _for_viewer(viewer, f"The booking couldn't be saved ({reason}). Nothing was held.",
                                              "The booking couldn't be saved. Nothing was held. Try again."), "not_saved") from None

    lead = f"{resolved.passengers[0].title or ''} {resolved.passengers[0].first_name or ''} {resolved.passengers[0].last_name or ''}".strip()
    document = flight_holds.customer_card_document(request.contact, resolved.passengers)  # the same rule as the Flights hold
    if request_only:  # a request: staff confirm the fare and hold it from the admin
        await notifications.booking_event("booking_requested", reference=reference, passenger=lead, route=ticket.sector,
                                          fare_minor=selling_each * request.adults, currency=currency, pnr=None, hold_expires_at=None,
                                          note="Website request: nothing is held at the supplier yet.")
        # B23: website customers are saved to (or matched with) Customers & Travelers too.
        await run_in_threadpool(flight_holds._save_people, resolved, reference, viewer.user_id, owner, links, document)
        return await read(reference, viewer)

    # 2. The hold. Never retried: a failure may still have held seats, so staff check the supplier.
    try:
        held = await provider.hold(ticket, resolved.passengers, resolved.contact)
    except SupplierRefused:
        await _mark_failed(booking_id, viewer, "refused")
        raise await _after_refusal(request, ticket, fetch) from None
    except Exception:  # noqa: BLE001 - unclear outcome, never retried
        logger.warning("Group hold failed", extra={"vendor": ticket.vendor, "reference": reference})
        await _mark_failed(booking_id, viewer, "not_confirmed")
        raise GroupHoldError(502, "The supplier didn't confirm the hold. Check with the supplier before trying again.", "not_confirmed_hold") from None

    # 3. Complete our record with the supplier's answer. When the supplier says what it held at (Dhillu & Sahi:
    #    total_amount), that is the cost. The selling price stays the staff's; if the cost moved, the new profit is
    #    shown and payments wait until it is confirmed (refused while the selling price is below the new cost).
    supplier_total_minor = held.cost_per_adult.amount_minor * request.adults
    if held.supplier_total is not None:
        supplier_total_minor = held.supplier_total.amount_minor
    selling = GroupMoney(amount_minor=selling_each, currency=currency)
    held_at = _now()
    expires_at = hold_expiry(held_at, provider)
    extra_hold_time: dict = {}
    if held.supplier_expires_at is not None:
        expires_at = held.supplier_expires_at
        extra_hold_time = {"hold_time": {"source": "supplier", "from": (held.booked_at or held_at).isoformat()}}
    elif getattr(provider, "hold_time_source", None):
        # The supplier gave no usable expiry: hold time (add-booking time) + the provider's default minutes.
        expires_at = hold_expiry(held.booked_at or held_at, provider)
        extra_hold_time = {"hold_time": {"source": "default", "minutes": hold_minutes(provider), "from": (held.booked_at or held_at).isoformat()}}
    extra: dict = dict(extra_hold_time)
    if held.supplier_answer is not None:
        extra["supplier_answer"] = held.supplier_answer
        if isinstance(held.supplier_answer.get("amounts"), dict):
            extra["supplier_amounts"] = {**held.supplier_answer["amounts"], "read_at": held_at.isoformat()}
    if supplier_total_minor != listing_cost.amount_minor * request.adults:
        extra["price_change"] = {"shown_cost_total_minor": listing_cost.amount_minor * request.adults,
                                 "held_cost_total_minor": supplier_total_minor, "accepted": False}
    try:
        await run_in_threadpool(lambda: store.confirm_hold(
            booking_id, viewer.user_id, pnr=held.pnr, supplier_ref=held.supplier_ref, hold_expires_at=expires_at,
            selling_total_minor=selling.amount_minor * request.adults, supplier_total_minor=supplier_total_minor,
            supplier_response={"held_at": held_at.isoformat(), "source": source_record(ticket, held.pnr), **extra},
            audit={"pnr": held.pnr, "time_limit": expires_at.isoformat(), "amount_minor": selling.amount_minor * request.adults,
                   "currency": selling.currency, "pax_count": request.adults},
        ))
    except Exception as error:  # noqa: BLE001 - the supplier holds seats we couldn't record: never lose the PNR
        raise await _unrecorded(booking_id, reference, held, provider, ticket.vendor, request.adults, viewer, error_reason(error)) from None
    await notifications.booking_event("hold_created", reference=reference, passenger=lead, route=ticket.sector,
                                      fare_minor=selling.amount_minor * request.adults, currency=selling.currency, pnr=held.pnr,
                                      hold_expires_at=expires_at)
    # B23: every booking saves (or matches) its customer and travelers in Customers & Travelers, website ones included
    # (same duplicate rules: a matching passport or CNIC links; a likely match is left for staff to choose).
    outcomes = await run_in_threadpool(flight_holds._save_people, resolved, reference, viewer.user_id, owner, links, document)
    if website:
        return await read(reference, viewer)
    view = await read(reference, viewer)
    view.saved_people = outcomes
    return view


# ---------- price check, release, issue ----------

def _selling_each(request: GroupHoldRequest, cost: GroupMoney, default: GroupMoney, viewer: BackOfficeUser) -> int:
    """The selling price per adult in minor units: the default unless the staff set one (Super Admin, or staff with
    record payments). Whole rupees, not below the supplier cost. Raises GroupHoldError (422) on a field."""
    chosen = request.selling_price_per_adult
    if chosen is None or (chosen.amount_minor == default.amount_minor and chosen.currency == default.currency):
        return default.amount_minor
    field = ["selling_price_per_adult"]
    if not viewer.can(RECORD_PAYMENTS):
        raise GroupHoldError(403, "Changing the selling price needs the record payments permission.", "not_allowed")
    if chosen.currency != cost.currency:
        raise GroupHoldError(422, f"The selling price must be in {cost.currency}.", "invalid_selling_price", [(field, "Wrong currency.")])
    try:
        pricing.check_selling(chosen.amount_minor, cost.amount_minor)
    except pricing.PriceRefused as error:
        raise GroupHoldError(422, error.message, error.code, [(field, error.message)]) from None
    return chosen.amount_minor


def _held_ref(booking: store.GroupBooking) -> HeldRef:
    cost = GroupMoney(amount_minor=booking.supplier_cost_minor // max(booking.adults, 1), currency=booking.currency) \
        if booking.supplier_cost_minor else None
    return HeldRef(booking.pnr, booking.supplier_ref or "", booking.adults, cost)


async def _open_hold(reference: str) -> tuple[store.GroupBooking, GroupHoldProvider]:
    try:
        booking = await run_in_threadpool(store.expire_if_over, reference, _now(), frozenset(NO_RELEASE_SOURCES))
    except store.GroupHoldRefused as error:
        raise _refused(error) from None
    booking = await refresh_supplier(booking)
    if booking.supplier_cancelled:
        raise GroupHoldError(409, "The supplier cancelled this booking. Search again and hold new seats.", "supplier_cancelled")
    status = _status(booking, _now())
    if status == "expired":
        raise GroupHoldError(409, "The hold has expired. Search again and hold new seats.", "hold_expired")
    if status != "held":
        raise GroupHoldError(409, "This booking isn't on hold.", "not_held")
    provider = provider_for(booking.provider)
    if provider is None or not booking.supplier_ref:
        raise GroupHoldError(409, "This hold can't be managed through Travelian. Check with the supplier.", "hold_not_supported")
    return booking, provider


async def _new_cost(booking: store.GroupBooking, provider: GroupHoldProvider) -> tuple[GroupMoney, int]:
    """(supplier cost per adult, supplier cost total) from the supplier. Read-only at the supplier. The total is the
    supplier's own (no remainder lost); the per-adult figure is only what the supplier's issue call takes."""
    try:
        total = await provider.price(_held_ref(booking))
    except PriceOrSeatsChanged:
        held = _held_ref(booking)
        raise (await _research(booking.ticket_id, booking.adults, held.cost_per_adult, None, "Nothing was changed.")
               or GroupHoldError(409, "The supplier says the price or the seats changed. Nothing was changed.", "price_changed")) from None
    except Exception:  # noqa: BLE001
        raise GroupHoldError(502, "The price couldn't be checked with the supplier. Nothing was changed.", "price_check_failed") from None
    if total.currency != booking.currency:
        raise GroupHoldError(409, "The supplier's price is in a different currency. Nothing was changed.", "currency_changed")
    return GroupMoney(amount_minor=pricing.split(total.amount_minor, max(booking.adults, 1))[-1], currency=total.currency), total.amount_minor


def _check(booking: store.GroupBooking, cost_total: int, viewer: BackOfficeUser) -> GroupPriceCheck:
    """The selling price stays; only the supplier cost (and so the profit) can change."""
    money = lambda minor: GroupMoney(amount_minor=minor, currency=booking.currency)  # noqa: E731
    check = GroupPriceCheck(changed=cost_total != booking.supplier_cost_minor, selling_total=money(booking.total_minor),
                            below_cost=cost_total > booking.total_minor, accepted_supplier_cost=money(cost_total))
    if viewer.visibility.cost_and_margin:
        previous = booking.supplier_cost_minor
        check.previous_supplier_cost = money(previous) if previous is not None else None
        check.new_supplier_cost = money(cost_total)
        check.difference = money(cost_total - previous) if previous is not None else None
        check.profit = money(booking.total_minor - cost_total)
    return check


async def price_check(reference: str, viewer: BackOfficeUser) -> GroupPriceCheck:
    booking, provider = await _open_hold(reference)
    _, cost_total = await _new_cost(booking, provider)
    return _check(booking, cost_total, viewer)


async def release(reference: str, viewer: BackOfficeUser) -> GroupHoldView:
    booking, provider = await _open_hold(reference)
    if booking.provider in NO_RELEASE_SOURCES:
        raise GroupHoldError(409, "This provider has no release call. The hold ends by itself when its time runs out.", "release_not_supported")
    if booking.issuance_status not in ("not_issued", "requested"):
        raise GroupHoldError(409, "Tickets are being issued for this booking. It can't be released.", "issuing")
    try:
        await provider.release(_held_ref(booking))
    except Exception as error:  # noqa: BLE001 - the expiry job retries; the booking reads as expired meanwhile
        code = getattr(error, "error_code", type(error).__name__)
        logger.warning("Group release failed; the expiry job will retry", extra={"reference": reference, "error_code": code})
        try:
            await run_in_threadpool(store.mark_release_failed_on_request, reference, viewer.user_id, code, _now())
        except store.GroupHoldRefused as refused:
            raise _refused(refused) from None
        return await read(reference, viewer)
    try:
        await run_in_threadpool(store.mark_released, reference, viewer.user_id, _now())
    except store.GroupHoldRefused as error:
        raise _refused(error) from None
    return await read(reference, viewer)


async def issue(reference: str, viewer: BackOfficeUser, accepted_cost: GroupMoney | None = None,
                accept_below_cost: bool = False) -> GroupIssueResult:
    if not viewer.can(ISSUE_TICKETS):
        raise GroupHoldError(403, "Issuing tickets requires the issue tickets permission.", "not_allowed")
    booking, provider = await _open_hold(reference)
    if not supports_issue(provider):
        raise GroupHoldError(409, f"{NO_ISSUE_REASON}. Nothing was issued.", "issue_not_supported")
    if booking.paid_minor < booking.total_minor:
        due = booking.total_minor - booking.paid_minor
        raise GroupHoldError(409, f"Record the full payment first: {money_text(due, booking.currency)} is still due. Nothing was issued.", "payment_due")

    # 1. Check the supplier cost again (read-only). The selling price never changes here. A different cost needs the
    #    issuer's confirmation, and an explicit one more when the cost is now above the selling price.
    cost, cost_total = await _new_cost(booking, provider)
    check = _check(booking, cost_total, viewer)
    if check.changed:
        confirmed = accepted_cost is not None and accepted_cost.currency == booking.currency and accepted_cost.amount_minor == cost_total
        if not confirmed or (check.below_cost and not accept_below_cost):
            detail = check.model_dump(mode="json", exclude_none=True, exclude={"changed"})
            if check.below_cost:
                raise GroupHoldError(409, "The supplier price is now above the selling price. Confirm to issue at a loss. Nothing was issued.",
                                     "below_cost", **detail)
            raise GroupHoldError(409, "The supplier price has changed. The selling price stays; confirm the new cost to issue. Nothing was issued.",
                                 "supplier_cost_changed", **detail)
        try:
            booking = await run_in_threadpool(store.accept_cost, reference, viewer.user_id, booking.supplier_cost_minor, cost_total)
        except store.GroupHoldRefused as error:
            raise _refused(error) from None

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


async def record_tickets(reference: str, viewer: BackOfficeUser, final_pnr: str, tickets: dict[int, str | None]) -> GroupHoldView:
    """Staff record the final PNR and ticket numbers (issue tickets permission, paid in full)."""
    if not viewer.can(ISSUE_TICKETS):
        raise GroupHoldError(403, "Recording the final PNR and tickets requires the issue tickets permission.", "not_allowed")
    try:
        booking = await run_in_threadpool(store.load, reference)
        if booking.booking_status == "held" and store.is_over(booking, _now()):
            raise GroupHoldError(409, "This hold has expired.", "hold_expired")
        await run_in_threadpool(store.record_tickets, reference, viewer.user_id, final_pnr, tickets)
    except store.GroupHoldRefused as error:
        raise _refused(error) from None
    return await read(reference, viewer)


__all__ = ["GroupHoldError", "GroupHoldProvider", "HeldRef", "HeldSeats", "NO_ISSUE_REASON", "PROVIDERS", "SUPPORT", "SupplierRefused",
           "hold", "hold_expiry", "issue", "price_check", "provider_for", "read", "record_tickets", "release", "supports_issue"]
