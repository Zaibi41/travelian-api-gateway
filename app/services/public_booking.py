"""Public website booking (no login): group ticketing first.

- Search: the combined group inventory, cached for PUBLIC_SEARCH_CACHE_SECONDS, shown with no permissions (selling
  price only; never supplier names, cost or PNR).
- Book: with PUBLIC_GROUP_BOOKING_ENABLED the gateway holds the seats at the supplier (the same hold staff use) and
  the guest can pay online; without it the booking is saved as a request ('requested'): no supplier call, no payment,
  staff follow up from the admin Bookings list.
- The guest gets the booking reference and a private token; both are needed to view or pay the booking.
- Pay: PayFast hosted checkout. A payment is recorded only from a PayFast result whose validation hash the gateway
  verified with its secured key, for the amount the gateway asked for (app.db.public_bookings.record_result).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.field_visibility import NONE, BackOfficeUser
from app.db import booking_people, group_holds as store, public_bookings
from app.schemas.group_holds import GroupHoldContact, GroupHoldPassenger, GroupHoldRequest
from app.schemas.group_ticketing import GroupInventory
from app.schemas.live_flights import HoldContact, HoldPassenger, HoldRequest
from app.services import group_holds, notifications
from app.services.group_ticketing import GroupInventoryUnavailable, fetch_all_group_inventory, filter_group_inventory
from app.services.group_ticketing_view import inventory_view
from app.vendors.payments import payfast

logger = logging.getLogger(__name__)

WEBSITE = BackOfficeUser(user_id=None, role=group_holds.WEBSITE_ROLE, visibility=NONE, actions=frozenset())  # type: ignore[arg-type]


class PublicError(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


# ---------- rate limit (per client IP; one gateway worker) ----------

class RateLimiter:
    def __init__(self, limit: int, window_seconds: float):
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 10000:  # bound memory
            for stale in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window]:
                del self._hits[stale]
        return True


SEARCH_LIMIT = RateLimiter(30, 60)  # searches per IP per minute
BOOK_LIMIT = RateLimiter(5, 3600)  # bookings per IP per hour
PAY_LIMIT = RateLimiter(20, 3600)  # payment starts and views per IP per hour


def check_rate(limiter: RateLimiter, ip: str) -> None:
    if not limiter.allow(ip or "unknown"):
        raise PublicError(429, "Too many requests. Please wait a few minutes and try again.", "rate_limited")


# ---------- search ----------

_cache: tuple[float, list[GroupInventory]] | None = None
_cache_lock = asyncio.Lock()


async def cached_inventory() -> list[GroupInventory]:
    global _cache
    ttl = settings.public_search_cache_seconds
    if _cache and _cache[0] > time.monotonic():
        return _cache[1]
    async with _cache_lock:
        if _cache and _cache[0] > time.monotonic():
            return _cache[1]
        try:
            groups = await fetch_all_group_inventory()
        except GroupInventoryUnavailable:
            raise PublicError(503, "Group fares can't be loaded right now. Please try again shortly.", "unavailable") from None
        _cache = (time.monotonic() + ttl, groups) if ttl else None
        return groups


#: Ticket fields for staff only, never on the public site: the Owned/Supplier stock label and the hold flags.
STAFF_ONLY_TICKET_FIELDS = {"stock", "can_hold", "hold_unavailable_reason"}


async def search(origin: str | None, destination: str | None, departure_date, adults: int) -> dict:
    groups = await cached_inventory()
    if origin or destination or departure_date:
        groups = filter_group_inventory(groups, origin=origin, destination=destination, departure_date=departure_date, adults=adults)
    view = inventory_view(groups, NONE).model_dump(mode="json", exclude_none=True)
    for group in view.get("data", []):
        for ticket in group.get("tickets", []):
            for field in STAFF_ONLY_TICKET_FIELDS:
                ticket.pop(field, None)
    return view


# ---------- flights search (Southair + SooperFare through the gateway's static IP) ----------

#: The public site's searches share one "website" identity; its search ids are unguessable and expire with the search.
WEBSITE_SEARCH_VIEWER = BackOfficeUser(user_id="website", role=group_holds.WEBSITE_ROLE, visibility=NONE, actions=frozenset())  # type: ignore[arg-type]
_flight_cache: dict[tuple, tuple[float, object]] = {}
_flight_locks: dict[tuple, asyncio.Lock] = {}


async def search_flights(request) -> object:
    """Live flights with selling prices only (no supplier names or cost). The same search within
    PUBLIC_SEARCH_CACHE_SECONDS is answered from the cache, so visitors don't each call the suppliers."""
    from app.services import live_flights

    key = (request.origin, request.destination, request.departure_date, request.return_date, request.adults,
           request.children, request.infants, request.cabin)
    now = time.monotonic()
    cached = _flight_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    lock = _flight_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _flight_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        view = await live_flights.search(request, WEBSITE_SEARCH_VIEWER)
        _remember_lowest(request, view)
        ttl = min(settings.public_search_cache_seconds, live_flights.SEARCH_TTL_SECONDS - 60)
        if view.status == "complete" and ttl > 0:
            _flight_cache[key] = (time.monotonic() + ttl, view)
        for stale in [k for k, (expires, _) in _flight_cache.items() if expires <= time.monotonic()]:
            _flight_cache.pop(stale, None)
            _flight_locks.pop(stale, None)
        return view


# ---------- flight fare calendar (real lowest fares per date, never guessed) ----------

#: (origin, destination, cabin, adults, date) -> (checked_at epoch, lowest selling price per adult in minor units, currency).
_fares_by_date: dict[tuple, tuple[float, int, str]] = {}
FARE_DATE_TTL_SECONDS = 30 * 60
CALENDAR_DAYS = 60
CALENDAR_REFRESH_DAYS = 14  # dates checked per calendar open (only those not already known)
_refresh_jobs: dict[tuple, asyncio.Task] = {}
_supplier_slots: asyncio.Semaphore | None = None  # at most 3 calendar date searches at once, across all routes


def _slots() -> asyncio.Semaphore:
    global _supplier_slots
    _supplier_slots = _supplier_slots or asyncio.Semaphore(3)
    return _supplier_slots


def _remember_lowest(request, view) -> None:
    """Keep the lowest selling price per adult of a completed one-way, adults-only search for its date."""
    if request.return_date or request.children or request.infants or getattr(view, "status", None) != "complete":
        return
    prices = [fare.selling_price for journey in view.journeys if journey.direction == "outbound"
              for flight in journey.flights for fare in flight.fares if fare.selling_price]
    key = (request.origin, request.destination, request.cabin, request.adults, request.departure_date)
    if prices:
        lowest = min(prices, key=lambda money: money.amount_minor)
        _fares_by_date[key] = (time.time(), lowest.amount_minor, lowest.currency)
    else:
        _fares_by_date[key] = (time.time(), 0, "")  # searched: no fares that day


class FlightCalendarDay(BaseModel):
    date: date
    checked: bool = Field(description="A real search for this date finished in the last 30 minutes.")
    available: bool
    lowest_price: PublicMoney | None = None
    checked_at: datetime | None = None


class FlightCalendar(BaseModel):
    origin: str
    destination: str
    days: list[FlightCalendarDay]
    pending: bool = Field(description="More dates are being checked: ask again in a few seconds.")


async def _check_dates(route: tuple, dates: list) -> None:
    from app.schemas.flights import FlightSearchRequest

    origin, destination, cabin, adults = route

    async def one(day) -> None:
        async with _slots():
            try:
                await search_flights(FlightSearchRequest(origin=origin, destination=destination, departure_date=day, adults=adults, cabin=cabin))
            except Exception as error:  # noqa: BLE001 - a date that fails stays unknown (no price)
                logger.info("Calendar date not checked", extra={"route": f"{origin}-{destination}", "error_type": type(error).__name__})

    await asyncio.gather(*(one(day) for day in dates))


async def flight_calendar(origin: str, destination: str, adults: int, cabin: str, start=None) -> FlightCalendar:
    """Every day of the next CALENDAR_DAYS: the lowest real selling price per adult from a search in the last 30 minutes,
    or no price. Up to CALENDAR_REFRESH_DAYS unknown days are searched in the background (3 at a time across all
    routes); the page asks again while `pending`."""
    today = datetime.now(timezone.utc).date()
    start = max(start or today, today)
    now = time.time()
    route = (origin, destination, cabin, adults)
    days, missing = [], []
    for offset in range(CALENDAR_DAYS):
        day = start + timedelta(days=offset)
        known = _fares_by_date.get((*route, day))
        fresh = known is not None and now - known[0] < FARE_DATE_TTL_SECONDS
        if not fresh and offset < CALENDAR_REFRESH_DAYS:
            missing.append(day)
        price = PublicMoney(amount_minor=known[1], currency=known[2]) if fresh and known[1] > 0 else None
        days.append(FlightCalendarDay(date=day, checked=fresh, available=price is not None, lowest_price=price,
                                      checked_at=datetime.fromtimestamp(known[0], timezone.utc) if fresh else None))
    job = _refresh_jobs.get(route)
    running = job is not None and not job.done()
    if missing and not running:
        _refresh_jobs[route] = asyncio.create_task(_check_dates(route, missing))
        running = True
    return FlightCalendar(origin=origin, destination=destination, days=days, pending=running)


# ---------- book ----------

class PublicGroupBookingRequest(BaseModel):
    """What a guest sends. The price is never taken from the guest: the gateway prices the fare itself."""

    ticket_id: str = Field(min_length=8, max_length=64, description="GroupTicketView.id from the public search.")
    adults: int = Field(ge=1, le=9)
    contact: GroupHoldContact
    passengers: list[GroupHoldPassenger] = Field(min_length=1, max_length=9)
    fare_terms_acknowledged: bool = Field(description="The guest agreed that this group fare is non-refundable and non-changeable.")
    payment_method: Literal["card", "bank_transfer", "jazzcash"] = Field(
        "card", description="card: PayFast online. bank_transfer: held at the supplier, paid to Travelian's bank account, "
                            "recorded by staff (then issued). jazzcash: off until enabled.")


class PublicMoney(BaseModel):
    amount_minor: int
    currency: str


class PublicLeg(BaseModel):
    flight_number: str | None = None
    origin: str | None = None
    destination: str | None = None
    departure_date: str | None = None
    departure_time: str | None = None
    arrival_time: str | None = None


class PublicBookingView(BaseModel):
    reference: str
    status: Literal["requested", "held", "confirmed", "issued", "expired", "released", "processing", "unknown", "not_recorded"]
    payment_status: Literal["unpaid", "part_paid", "paid"]
    live: bool = Field(description="True when seats are held at the airline's supplier; false for a request staff will follow up.")
    can_pay: bool = Field(description="Online payment can be started now.")
    total: PublicMoney
    paid: PublicMoney
    hold_expires_at: datetime | None = None
    airline: str | None = None
    route: str | None = None
    legs: list[PublicLeg] = []
    travelers: list[str] = Field(default_factory=list, description="Names only.")
    final_pnr: str | None = Field(None, description="The airline booking reference once ticketed.")
    tickets: list[str] = Field(default_factory=list, description="Ticket numbers once issued.")


class PublicBookingCreated(BaseModel):
    reference: str
    access_token: str = Field(description="Private. Needed with the reference to view or pay this booking. Shown once.")
    booking: PublicBookingView


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- payment methods offered at checkout ----------

class BankAccountView(BaseModel):
    """The account customers pay into, in full (D40): masking never applies to Travelian's receiving account."""
    bank_name: str | None = None
    account_title: str | None = None
    account_number: str | None = None
    iban: str | None = None
    qr_url: str | None = None


class PaymentMethodsView(BaseModel):
    card: bool
    bank_transfer: bool
    jazzcash: bool
    bank_accounts: list[BankAccountView] = []
    logo_url: str | None = None


def bank_accounts() -> list[BankAccountView]:
    from app.db import payment_accounts
    from app.services import assets

    # Only accounts a customer can actually pay into: a title plus the full number, IBAN or a QR image.
    return [BankAccountView(bank_name=a.bank_name, account_title=a.account_title, account_number=a.account_number, iban=a.iban,
                            qr_url=assets.public_url(a.qr_asset_key))
            for a in payment_accounts.list_accounts()
            if a.method == "bank_transfer" and a.account_title and (a.account_number or a.iban or a.qr_asset_key)]


async def payment_methods() -> PaymentMethodsView:
    from app.api.routes.admin_uploads import site_assets
    from app.vendors.payments import jazzcash

    accounts = await run_in_threadpool(bank_accounts) if settings.bank_transfer_enabled else []
    try:
        logo = (await run_in_threadpool(site_assets)).get("site_logo")
    except Exception:  # noqa: BLE001 - the logo is optional
        logo = None
    return PaymentMethodsView(card=payfast.configured(), bank_transfer=bool(accounts), jazzcash=jazzcash.enabled(),
                              bank_accounts=accounts, logo_url=logo)


async def book_group(request: PublicGroupBookingRequest) -> PublicBookingCreated:
    hold_request = GroupHoldRequest(
        ticket_id=request.ticket_id, adults=request.adults, contact=request.contact, passengers=request.passengers,
        fare_terms_acknowledged=request.fare_terms_acknowledged, selling_price_per_adult=None,
    )
    live = settings.public_group_booking_enabled
    if live and request.payment_method == "jazzcash":
        from app.vendors.payments import jazzcash

        if not jazzcash.enabled():
            raise PublicError(409, "JazzCash isn't available yet. Choose card or bank transfer.", "method_unavailable")
    if live and request.payment_method == "bank_transfer" and not (await payment_methods()).bank_transfer:
        raise PublicError(409, "Bank transfer isn't available right now. Choose card.", "method_unavailable")
    try:
        view = await group_holds.hold(hold_request, WEBSITE, request_only=not live)
    except group_holds.GroupHoldError as error:
        raise PublicError(error.status, error.message, error.code) from None
    token, token_hash = public_bookings.new_token()
    booking = await run_in_threadpool(store.load, view.reference)
    await run_in_threadpool(public_bookings.mark_website, booking.id, token_hash)
    # D43: bank transfer and JazzCash never confirm anything themselves. The booking stays held and staff are told (in the
    # admin and by email) to check the money arrived, record it, and only then issue.
    if live and request.payment_method in ("bank_transfer", "jazzcash"):
        lead = request.passengers[0]
        method = "bank transfer" if request.payment_method == "bank_transfer" else "JazzCash"
        await notifications.booking_event(
            "bank_transfer_hold", reference=view.reference, passenger=f"{lead.title or ''} {lead.first_name} {lead.last_name}".strip(),
            route=booking.itinerary[0].get("sector") if booking.itinerary and isinstance(booking.itinerary[0], dict) else None,
            fare_minor=booking.total_minor, currency=booking.currency, pnr=booking.pnr, hold_expires_at=booking.hold_expires_at,
            note=f"Customer chose {method}. Check the {method} payment has arrived, record it in the Payments tab, then issue. "
                 "Nothing is issued automatically. The hold keeps the supplier's own expiry (D29).")
    return PublicBookingCreated(reference=view.reference, access_token=token, booking=await booking_view(view.reference))


class PublicFlightRequest(BaseModel):
    """A website flight booking (B19, D43). It is saved as a request: nothing is held or ticketed, no PNR is given and no
    payment is taken here. The gateway reprices the fare itself (the price is never taken from the page); staff confirm
    the fare and payment with the customer, then book and issue from the admin."""

    search_id: str = Field(min_length=8, max_length=64, description="From POST /v1/public/flights/search.")
    fare_ids: list[str] = Field(min_length=1, max_length=2, description="The chosen fare, and its return fare for a round trip.")
    contact: HoldContact = Field(description="Name, phone (code and number) and email: all required.")
    passengers: list[HoldPassenger] = Field(min_length=1, max_length=9)

    @model_validator(mode="after")
    def guest_details(self):
        if self.contact.customer_id or any(p.traveler_id or p.document_id for p in self.passengers):
            raise ValueError("Enter the contact and traveler details.")
        missing = [label for label, value in (("first name", self.contact.first_name), ("last name", self.contact.last_name),
                                               ("phone code", self.contact.phone_code), ("phone", self.contact.phone),
                                               ("email", self.contact.email)) if not value]
        if missing:
            raise ValueError(f"Enter the contact's {', '.join(missing)}.")
        return self


async def request_flight(request: PublicFlightRequest) -> PublicBookingCreated:
    from app.services import flight_holds, flight_requests, live_flights

    try:
        priced = await live_flights.reprice(request.search_id, request.fare_ids, WEBSITE_SEARCH_VIEWER)
    except live_flights.RepriceError as error:
        raise PublicError(error.status if error.status < 500 else 502, error.message, error.code) from None
    hold_request = HoldRequest(quote_id=priced.quote_id, confirmed_total=priced.selling_total, contact=request.contact,
                               passengers=request.passengers, accept_price_change=True)
    try:
        view = await flight_requests.create(hold_request, WEBSITE_SEARCH_VIEWER, channel="website")
    except flight_holds.HoldError as error:
        raise PublicError(error.status, error.message, error.code) from None
    token, token_hash = public_bookings.new_token()
    await run_in_threadpool(public_bookings.mark_website_reference, view.booking_id, token_hash)
    return PublicBookingCreated(reference=view.booking_id, access_token=token, booking=await booking_view(view.booking_id))


async def _other_view(row: dict) -> PublicBookingView:
    """The guest's view of a non-group booking (website flight requests): no PNR or tickets until issued."""
    now = _now()
    status = lookup_status(row, now)
    view_status = {"requested": "requested", "issued": "issued", "expired": "expired", "cancelled": "released",
                   "paid": "confirmed"}.get(status, "held")
    people = await run_in_threadpool(booking_people.read, row["reference"])
    legs = []
    for leg in row["itinerary"] or []:
        if not isinstance(leg, dict):
            continue
        departs, arrives = str(leg.get("departure_local") or ""), str(leg.get("arrival_local") or "")
        legs.append(PublicLeg(flight_number=leg.get("flight_number"), origin=leg.get("origin"), destination=leg.get("destination"),
                              departure_date=departs[:10] or None, departure_time=departs[11:16] or None, arrival_time=arrives[11:16] or None))
    first = (row["itinerary"] or [{}])[0] if isinstance((row["itinerary"] or [{}])[0], dict) else {}
    issued = view_status == "issued"
    return PublicBookingView(
        reference=row["reference"], status=view_status, payment_status=row["payment_status"],  # type: ignore[arg-type]
        live=row["booking_status"] != "requested", can_pay=False,
        total=PublicMoney(amount_minor=row["total_minor"], currency=row["currency"]),
        paid=PublicMoney(amount_minor=row["paid_minor"], currency=row["currency"]),
        hold_expires_at=row["hold_expires_at"] if view_status == "held" else None,
        airline=first.get("airline_name"), route=f"{first.get('origin')}-{first.get('destination')}" if first.get("origin") else None,
        legs=legs, travelers=[f"{t.title} {t.first_name} {t.last_name}".strip() for t in (people.travelers if people else [])],
        final_pnr=(row["final_pnr"] or row["pnr"]) if issued else None,
        tickets=[t.ticket_number for t in (people.travelers if people else []) if t.ticket_number] if issued else [],
    )


async def booking_view(reference: str) -> PublicBookingView:
    row = await run_in_threadpool(public_bookings.summary, reference)
    if row is not None and row["product"] != "group":
        return await _other_view(row)
    now = _now()
    booking = await run_in_threadpool(store.expire_if_over, reference, now, frozenset(group_holds.NO_RELEASE_SOURCES))
    people = await run_in_threadpool(booking_people.read, reference)
    status = group_holds._status(booking, now)
    legs = [PublicLeg(flight_number=leg.get("flight_number"), origin=leg.get("origin"), destination=leg.get("destination"),
                      departure_date=leg.get("departure_date") or leg.get("date"), departure_time=leg.get("departure_time"),
                      arrival_time=leg.get("arrival_time")) for leg in booking.itinerary if isinstance(leg, dict)]
    first = booking.itinerary[0] if booking.itinerary and isinstance(booking.itinerary[0], dict) else {}
    travelers = [f"{t.title} {t.first_name} {t.last_name}".strip() for t in (people.travelers if people else [])]
    tickets = [t.ticket_number for t in (people.travelers if people else []) if t.ticket_number] if status == "issued" else []
    can_pay = (status == "held" and booking.payment_status != "paid" and payfast.configured()
               and not booking.price_change.get("accepted") is False and booking.booking_status == "held")
    return PublicBookingView(
        reference=booking.reference, status=status, payment_status=booking.payment_status,  # type: ignore[arg-type]
        live=booking.booking_status != "requested", can_pay=can_pay,
        total=PublicMoney(amount_minor=booking.total_minor, currency=booking.currency),
        paid=PublicMoney(amount_minor=booking.paid_minor, currency=booking.currency),
        hold_expires_at=booking.hold_expires_at if status == "held" else None,
        airline=first.get("airline_name") or first.get("airline"), route=first.get("sector"),
        legs=legs, travelers=travelers,
        final_pnr=booking.final_pnr if status == "issued" else None, tickets=tickets,
    )


async def open_booking(reference: str, token: str) -> PublicBookingView:
    if not await run_in_threadpool(public_bookings.guest_can_open, reference, token):
        raise PublicError(404, "Booking not found. Check the reference and the link from your confirmation.", "not_found")
    return await booking_view(reference)


# ---------- self-service lookup (B11, D37): reference + email or phone, read-only ----------

LOOKUP_LIMIT = RateLimiter(10, 3600)  # lookups per IP per hour (stops guessing references)
NEXT_STEPS = {
    "requested": "Travelian's team will contact you to confirm the fare and arrange payment. Nothing is held or charged yet.",
    "held": "Your seats are held. Pay before the hold time ends, or the booking expires and you'll need to search again.",
    "paid": "Payment received. Travelian is issuing your tickets; you'll receive them once they're ready.",
    "issued": "Your tickets are issued. Carry your passport or CNIC and arrive at the airport in good time.",
    "expired": "The hold ended before payment, so the seats were released. Please search again to book.",
    "cancelled": "This booking was cancelled.",
}


class LookupView(BaseModel):
    reference: str
    product: str
    status: Literal["requested", "held", "paid", "issued", "expired", "cancelled"]
    payment_status: Literal["unpaid", "part_paid", "paid"]
    total: PublicMoney
    paid: PublicMoney
    hold_expires_at: datetime | None = None
    legs: list[PublicLeg] = []
    travelers: list[str] = []
    airline_pnr: str | None = Field(None, description="Once tickets are issued.")
    tickets: list[str] = []
    next_steps: str


def lookup_status(row: dict, now: datetime) -> str:
    booking, issuance = row["booking_status"], row["issuance_status"]
    if booking == "requested":
        return "requested"
    if booking == "ticket_issued" or issuance == "issued":
        return "issued"
    if booking in ("cancelled",):
        return "cancelled"
    if booking == "expired" or (booking in ("held", "pending_payment") and row["hold_expires_at"] and row["hold_expires_at"] <= now
                                and row["payment_status"] != "paid"):
        return "expired"
    if row["payment_status"] == "paid" or booking in ("payment_received", "confirmed"):
        return "paid"
    return "held"


async def lookup(reference: str, contact: str) -> LookupView:
    row = await run_in_threadpool(public_bookings.lookup, reference, contact)
    if row is None:
        raise PublicError(404, "We couldn't find a booking with that reference and email or phone. Check both and try again.", "not_found")
    status = lookup_status(row, _now())
    legs = []
    for leg in row["itinerary"] or []:
        if not isinstance(leg, dict):
            continue
        departure = str(leg.get("departure_local") or "")
        legs.append(PublicLeg(flight_number=leg.get("flight_number"), origin=leg.get("origin"), destination=leg.get("destination"),
                              departure_date=leg.get("departure_date") or (departure[:10] or None),
                              departure_time=leg.get("departure_time") or (departure[11:16] or None),
                              arrival_time=leg.get("arrival_time") or (str(leg.get("arrival_local") or "")[11:16] or None)))
    issued = status == "issued"
    return LookupView(
        reference=row["reference"], product=row["product"], status=status, payment_status=row["payment_status"],
        total=PublicMoney(amount_minor=row["total_minor"], currency=row["currency"]),
        paid=PublicMoney(amount_minor=row["paid_minor"], currency=row["currency"]),
        hold_expires_at=row["hold_expires_at"] if status == "held" else None, legs=legs,
        travelers=[" ".join(filter(None, (t["title"], t["first_name"], t["last_name"]))) for t in row["travelers"]],
        airline_pnr=(row["final_pnr"] or row["pnr"]) if issued else None,
        tickets=[t["ticket_number"] for t in row["travelers"] if t["ticket_number"]] if issued else [],
        next_steps=NEXT_STEPS[status],
    )


# ---------- pay ----------

class CheckoutView(BaseModel):
    action: str = Field(description="PayFast's checkout URL: the browser posts `fields` there as a form.")
    fields: dict[str, str]
    sandbox: bool


async def start_payment(reference: str, token: str) -> CheckoutView:
    view = await open_booking(reference, token)
    if not payfast.configured():
        raise PublicError(503, "Online payment isn't available right now.", "payments_off")
    if not view.can_pay:
        raise PublicError(409, "This booking can't be paid online now." if view.live else
                          "This is a booking request. Travelian will confirm the fare before payment.", "not_payable")
    await group_holds.refresh_for_payment(reference)  # a supplier cancellation blocks the payment
    view = await booking_view(reference)
    if not view.can_pay:
        raise PublicError(409, "This booking can't be paid online now.", "not_payable")
    due = view.total.amount_minor - view.paid.amount_minor
    if due <= 0 or view.total.currency != "PKR":
        raise PublicError(409, "Nothing is due on this booking.", "not_payable")
    people = await run_in_threadpool(booking_people.read, reference)
    customer = people.customer if people else None
    basket_id = payfast.new_basket_id(reference)
    try:
        payfast_token = await payfast.access_token(basket_id, due, "PKR")
    except payfast.PayFastError as error:
        raise PublicError(503, error.message, "payments_unavailable") from None
    await run_in_threadpool(public_bookings.create_attempt, reference, basket_id, due, "PKR")
    return_url = f"{settings.public_site_url}/my-bookings?ref={quote(reference)}"
    form = payfast.checkout_form(
        token=payfast_token, basket_id=basket_id, amount_minor=due, currency="PKR",
        description=f"Travelian booking {reference}", mobile=getattr(customer, "phone", "") or "",
        email=getattr(customer, "email", "") or "", success_url=return_url, failure_url=return_url,
        notify_url=f"{settings.public_api_url}/v1/public/payments/payfast/notify", order_date=_now(),
    )
    return CheckoutView(action=form.action, fields=form.fields, sandbox=payfast.sandbox())


async def payfast_result(params: dict[str, str], source: str) -> public_bookings.Outcome | None:
    """Apply a PayFast result (its server notification, or the parameters the customer's browser came back with).
    None when the validation hash doesn't verify: nothing is changed."""
    if not payfast.configured():
        return None
    result = payfast.verify(params)
    if result is None:
        logger.warning("PayFast result with an invalid validation hash ignored", extra={"source": source})
        return None
    outcome = await run_in_threadpool(
        public_bookings.record_result, result.basket_id, paid=result.paid, err_code=result.err_code,
        transaction_id=result.transaction_id, amount_minor=result.amount_minor, message=result.message, source=source, now=_now(),
    )
    logger.info("PayFast result applied", extra={"source": source, "outcome": outcome.status, "reference": outcome.reference})
    return outcome
