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
import re
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
from app.schemas.flights import MAX_LEGS
from app.schemas.group_holds import GroupHoldContact, GroupHoldPassenger, GroupHoldRequest
from app.schemas.group_ticketing import GroupInventory
from app.schemas.live_flights import HoldContact, HoldPassenger, HoldRequest
from app.services import group_holds, notifications
from app.services.group_ticketing import GroupInventoryUnavailable, fetch_all_group_inventory, filter_group_inventory
from app.services.group_ticketing_view import inventory_view
from app.services import customer_emails
from app.vendors.payments import alfalah, payfast

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
# B24 / D46: fare calendars only read what searches already found (the gateway checks missing dates itself, a few at a
# time), so a page asking again while dates fill in doesn't use up the live-search allowance.
CALENDAR_LIMIT = RateLimiter(120, 60)  # calendar reads per IP per minute
BOOK_LIMIT = RateLimiter(5, 3600)  # bookings per IP per hour
PAY_LIMIT = RateLimiter(20, 3600)  # payment starts and views per IP per hour
QR_LIMIT = RateLimiter(30, 3600)  # QR downloads per IP per hour
CONTACT_LIMIT = RateLimiter(5, 3600)  # gateway#114: Contact page messages per IP per hour
DOCUMENTS_LIMIT = RateLimiter(20, 3600)  # checkout document checks per IP per hour (each new selection asks the supplier once)


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
    return lowest_fare_per_flight(view)


def _flight_identity(ticket: dict) -> tuple | None:
    """The same flights (travelian#228): every leg's direction, flight number, airports, date and time. Without legs, the
    ticket's flight number, date and sector. None when there's too little to tell two tickets apart (never merged)."""
    legs = ticket.get("legs") or []
    if legs:
        key = tuple((leg.get("direction"), str(leg.get("flight_number") or "").replace(" ", "").upper(), leg.get("origin"),
                     leg.get("destination"), leg.get("departure_date"), leg.get("departure_time")) for leg in legs)
        return key if all(part[1] and part[2] and part[3] and part[4] for part in key) else None
    number = str(ticket.get("flight_number") or "").replace(" ", "").upper()
    if number and ticket.get("departure_date") and ticket.get("sector"):
        return ("ticket", number, ticket.get("departure_date"), ticket.get("departure_time"), str(ticket["sector"]).upper())
    return None


def lowest_fare_per_flight(view: dict) -> dict:
    """travelian#228: a group flight offered by more than one supplier is shown once, with its lowest selling price."""
    best: dict[tuple, tuple[int, int]] = {}  # identity -> (price, id of the ticket kept)
    for group in view.get("data", []):
        for ticket in group.get("tickets", []):
            identity, price = _flight_identity(ticket), (ticket.get("selling_price") or {}).get("amount_minor")
            if identity is None or not isinstance(price, int):
                continue
            if identity not in best or price < best[identity][0]:
                best[identity] = (price, id(ticket))
    kept = {ticket_id for _, ticket_id in best.values()}
    for group in view.get("data", []):
        group["tickets"] = [t for t in group.get("tickets", []) if _flight_identity(t) is None
                            or not isinstance((t.get("selling_price") or {}).get("amount_minor"), int) or id(t) in kept]
    view["data"] = [group for group in view.get("data", []) if group.get("tickets")]
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
           request.children, request.infants, request.cabin,
           tuple((leg.origin, leg.destination, leg.departure_date) for leg in request.legs or ()))
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
    if request.return_date or request.legs or request.children or request.infants or getattr(view, "status", None) != "complete":
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
    status: Literal["requested", "held", "confirmed", "issued", "expired", "released", "processing", "unknown", "not_recorded",
                    "paid_not_ticketed"] = Field(description="paid_not_ticketed: paid, and Travelian's team will contact the customer.")
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
    card_fee: "CardFeeView | None" = Field(None, description="When card / 1Link payment is possible: the fee on the amount due and the total "
                                                              "PayFast will charge. Calculated by the gateway (travelian#74).")
    chosen_account_id: str | None = Field(None, description="The receiving account the customer chose for bank transfer / JazzCash.")


class CardFeeView(BaseModel):
    percent: str = Field(description='e.g. "3%"')
    fee: PublicMoney
    total: PublicMoney = Field(description="Amount due plus the fee: what the card is charged.")


PublicBookingView.model_rebuild()


class PublicBookingCreated(BaseModel):
    reference: str
    access_token: str = Field(description="Private. Needed with the reference to view or pay this booking. Shown once.")
    booking: PublicBookingView


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- payment methods offered at checkout ----------

class BankAccountView(BaseModel):
    """The account customers pay into, in full (D40): masking never applies to Travelian's receiving account."""
    id: str | None = Field(None, description="Send it back as the chosen account (travelian#76).")
    bank_name: str | None = None
    account_title: str | None = None
    account_number: str | None = None
    iban: str | None = None
    qr_url: str | None = None
    qr_download_url: str | None = Field(None, description="Gateway path that downloads the QR image as a file (every browser, no S3 CORS).")
    logo_url: str | None = Field(None, description="The bank's logo uploaded by staff (travelian#76). None: show the bank's initials.")


class PaymentMethodsView(BaseModel):
    card: bool = Field(description="Card / 1Link through PayFast: on production PayFast, or UAT with PAYFAST_ALLOW_UAT (testing).")
    alfalah: bool = Field(False, description="Card on Bank Alfalah's page (Alfa Payment Gateway): on production APG, or the sandbox "
                                             "with ALFALAH_ALLOW_SANDBOX (test money).")
    bank_transfer: bool = Field(description="Only when an active bank account with a full account number exists.")
    jazzcash: bool = Field(description="Only with JAZZCASH_ENABLED and an active JazzCash account with a full account number.")
    flights: bool = Field(False, description="Website flight fares can be held and paid here (card, bank transfer, JazzCash as above). "
                                             "False: a flight booking is a request and Travelian arranges payment.")
    bank_accounts: list[BankAccountView] = []
    jazzcash_accounts: list[BankAccountView] = Field(default_factory=list, description="Where customers send a JazzCash payment.")
    logo_url: str | None = None


def _qr_path(account) -> str | None:
    from app.services import assets

    return f"/v1/public/payment-qr/{account.id}" if account.qr_asset_key and assets.public_url(account.qr_asset_key) else None


QR_MAX_BYTES = 5 * 1024 * 1024
QR_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


async def payment_qr(account_id: str, client_factory=None) -> tuple[bytes, str, str]:
    """The QR image of an account checkout shows (bank transfer or JazzCash), as (bytes, content type, file name).
    Read from its public S3 URL (no credentials); only images, at most 5 MB."""
    import httpx

    from app.db import payment_accounts
    from app.services import assets

    shown = {a.id: a for a in payment_accounts.list_accounts() if a.method in ("bank_transfer", "jazzcash")}
    account = shown.get(account_id)
    url = assets.public_url(account.qr_asset_key) if account else None
    if account is None or url is None:
        raise PublicError(404, "This QR code isn't available.", "not_found")
    try:
        async with (client_factory or httpx.AsyncClient)(timeout=10, follow_redirects=False) as client:
            answer = await client.get(url)
    except httpx.HTTPError:
        raise PublicError(502, "The QR image couldn't be loaded. Try again, or scan it from the screen.", "qr_unavailable") from None
    kind = (answer.headers.get("content-type") or "").split(";")[0].strip().lower()
    if answer.status_code != 200 or kind not in QR_TYPES or len(answer.content) > QR_MAX_BYTES:
        raise PublicError(502, "The QR image couldn't be loaded. Try again, or scan it from the screen.", "qr_unavailable")
    name = re.sub(r"[^a-z0-9]+", "-", (account.bank_name or account.method).lower()).strip("-") or "payment"
    return answer.content, kind, f"travelian-{name}-qr.{QR_TYPES[kind]}"


def bank_accounts() -> list[BankAccountView]:
    from app.db import payment_accounts
    from app.services import assets

    # Only accounts a customer can actually pay into: a title plus the full number, IBAN or a QR image.
    return [BankAccountView(id=getattr(a, "id", None), bank_name=a.bank_name, account_title=a.account_title, account_number=a.account_number, iban=a.iban,
                            qr_url=assets.public_url(a.qr_asset_key), qr_download_url=_qr_path(a),
                            logo_url=assets.public_url(getattr(a, "logo_asset_key", None)))
            for a in payment_accounts.list_accounts()
            if a.method == "bank_transfer" and a.account_title and (a.account_number or a.iban or a.qr_asset_key)]


def jazzcash_accounts() -> list[BankAccountView]:
    """Active JazzCash receiving accounts with the full account (wallet) number customers pay into, and its QR image if any."""
    from app.db import payment_accounts
    from app.services import assets

    return [BankAccountView(id=getattr(a, "id", None), bank_name="JazzCash", account_title=a.account_title or a.label, account_number=a.account_number,
                            qr_url=assets.public_url(a.qr_asset_key), qr_download_url=_qr_path(a),
                            logo_url=assets.public_url(getattr(a, "logo_asset_key", None)))
            for a in payment_accounts.list_accounts() if a.method == "jazzcash" and a.account_number]


async def payment_methods() -> PaymentMethodsView:
    """What checkout may offer. Each method only when it can really be paid: card only on production PayFast; bank transfer
    and JazzCash only with an active receiving account that has the full account number (Accounts & Ledgers)."""
    from app.api.routes.admin_uploads import site_assets

    accounts = await run_in_threadpool(bank_accounts) if settings.bank_transfer_enabled else []
    wallets = await run_in_threadpool(jazzcash_accounts) if settings.jazzcash_enabled else []
    try:
        logo = (await run_in_threadpool(site_assets)).get("site_logo")
    except Exception:  # noqa: BLE001 - the logo is optional
        logo = None
    return PaymentMethodsView(card=payfast.payments_allowed(), alfalah=alfalah.payments_allowed(), bank_transfer=any(a.account_number for a in accounts), jazzcash=bool(wallets),
                              flights=settings.public_flight_booking_enabled, bank_accounts=accounts, jazzcash_accounts=wallets, logo_url=logo)


async def book_group(request: PublicGroupBookingRequest) -> PublicBookingCreated:
    hold_request = GroupHoldRequest(
        ticket_id=request.ticket_id, adults=request.adults, contact=request.contact, passengers=request.passengers,
        fare_terms_acknowledged=request.fare_terms_acknowledged, selling_price_per_adult=None,
    )
    live = settings.public_group_booking_enabled
    if live and request.payment_method == "jazzcash" and not (await payment_methods()).jazzcash:
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
    if live:
        await customer_emails.send(view.reference, HOLD_EMAIL.get(request.payment_method or "card", "booking_held"))
    return PublicBookingCreated(reference=view.reference, access_token=token, booking=await booking_view(view.reference))


class PublicFlightRequest(BaseModel):
    """A website flight booking (B19, D43). The gateway reprices the fare itself (the price is never taken from the page).
    With PUBLIC_FLIGHT_BOOKING_ENABLED off, or for a restricted airline (D32), or when booking is off for the fare's
    provider, it is saved as a request: nothing is held or ticketed, no PNR is given and no payment is taken; staff
    confirm the fare and payment with the customer. Otherwise the fare is held at the supplier and paid by
    `payment_method` (card: PayFast, then issued automatically when ticketing is on, D48; bank transfer and JazzCash:
    held until staff verify the money and issue by hand, D49)."""

    search_id: str = Field(min_length=8, max_length=64, description="From POST /v1/public/flights/search.")
    fare_ids: list[str] = Field(min_length=1, max_length=MAX_LEGS, description="The chosen fare, and its return fare for a round trip; "
                                                                             "multi-city: one fare per leg, in leg order.")
    contact: HoldContact = Field(description="Name, phone (code and number) and email: all required.")
    passengers: list[HoldPassenger] = Field(min_length=1, max_length=9)
    payment_method: Literal["card", "bank_transfer", "jazzcash", "book_only"] = Field(
        "card", description="Used when the fare is held (see above). card: PayFast online. bank_transfer / jazzcash: staff verify the payment and issue. book_only: held now, paid "
                            "later by arrangement with Travelian's team. Nothing but a verified card payment is ever issued automatically.")

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


def _flight_hold_allowed(quote) -> bool:
    """The website may hold this fare at the supplier: switched on, booking on for its provider, not a restricted airline."""
    from app.services import live_flights, restricted_airlines

    return (settings.public_flight_booking_enabled and quote is not None and not quote.request.multi_city
            and live_flights.booking_enabled(quote.provider) and not any(restricted_airlines.segment_restricted(ref.segment) for ref in quote.refs))


def card_allowed(provider: str | None) -> bool:
    """Online payment (card / 1Link through PayFast, or card through Bank Alfalah) for every flight supplier, only while one
    of them may take payments (production, or its test mode switched on). Where the gateway
    can't issue a supplier's tickets yet (SooperFare), a paid booking goes to staff with a red alert."""
    return payfast.payments_allowed() or alfalah.payments_allowed()


class FlightPaymentOptions(BaseModel):
    held: bool = Field(description="The fare will be held at the airline and paid here. False: it is sent as a request (no payment).")
    card: bool = Field(description="Card / 1Link through PayFast.")
    alfalah: bool = Field(False, description="Card through Bank Alfalah (Alfa Payment Gateway).")
    bank_transfer: bool
    jazzcash: bool


async def flight_payment_options(search_id: str, fare_ids: list[str]) -> FlightPaymentOptions:
    """What checkout may offer for these fares (decided here, never by the page). Reads the search; no supplier call."""
    from app.services import live_flights, restricted_airlines

    session = live_flights.get_session(search_id, WEBSITE_SEARCH_VIEWER.user_id)
    if session is None:
        raise PublicError(404, "This search has expired. Search again.", "search_expired")
    try:
        refs = live_flights._selected_refs(session, fare_ids)
    except live_flights.RepriceError as error:
        raise PublicError(error.status, error.message, error.code) from None
    provider = refs[0].provider
    held = (settings.public_flight_booking_enabled and not session.request.multi_city and live_flights.booking_enabled(provider)
            and not any(restricted_airlines.segment_restricted(ref.segment) for ref in refs))
    if not held:
        return FlightPaymentOptions(held=False, card=False, bank_transfer=False, jazzcash=False)
    methods = await payment_methods()
    online = card_allowed(provider)
    return FlightPaymentOptions(held=True, card=online and payfast.payments_allowed(), alfalah=online and alfalah.payments_allowed(),
                                bank_transfer=methods.bank_transfer, jazzcash=methods.jazzcash)


#: A hold failure the guest can act on is shown as is; anything else (supplier errors, a hold that couldn't be verified or
#: saved) gets one plain sentence: no supplier names, PNRs or staff instructions on the public site.
HOLD_NOT_PLACED = "We couldn't hold this fare with the airline, and nothing was charged. Please search again or choose another fare."


async def request_flight(request: PublicFlightRequest) -> PublicBookingCreated:
    from app.services import flight_holds, flight_requests, live_flights

    try:
        priced = await live_flights.reprice(request.search_id, request.fare_ids, WEBSITE_SEARCH_VIEWER)
    except live_flights.RepriceError as error:
        raise PublicError(error.status if error.status < 500 else 502, error.message, error.code) from None
    hold_request = HoldRequest(quote_id=priced.quote_id, confirmed_total=priced.selling_total, contact=request.contact,
                               passengers=request.passengers, accept_price_change=True)
    if not _flight_hold_allowed(live_flights.get_quote(priced.quote_id, WEBSITE_SEARCH_VIEWER.user_id)):
        try:
            view = await flight_requests.create(hold_request, WEBSITE_SEARCH_VIEWER, channel="website")
        except flight_holds.HoldError as error:
            raise PublicError(error.status, error.message, error.code) from None
        reference = view.booking_id
    else:
        reference = await _hold_flight(hold_request, request, live_flights.get_quote(priced.quote_id, WEBSITE_SEARCH_VIEWER.user_id))
    token, token_hash = public_bookings.new_token()
    await run_in_threadpool(public_bookings.mark_website_reference, reference, token_hash)
    return PublicBookingCreated(reference=reference, access_token=token, booking=await booking_view(reference))


async def _hold_flight(hold_request: HoldRequest, request: PublicFlightRequest, quote) -> str:
    """Hold the fare at the supplier for a guest (the same hold staff use). Payment comes after; no ticket is issued here."""
    from app.services import flight_holds

    method = request.payment_method
    if method == "card" and not card_allowed(quote.provider):
        raise PublicError(409, "Card payment isn't available right now. Choose another payment method.", "method_unavailable")
    if method == "jazzcash" and not (await payment_methods()).jazzcash:
        raise PublicError(409, "JazzCash isn't available yet. Choose card or bank transfer.", "method_unavailable")
    if method == "bank_transfer" and not (await payment_methods()).bank_transfer:
        raise PublicError(409, "Bank transfer isn't available right now. Choose card.", "method_unavailable")
    try:
        view = await flight_holds.hold(hold_request, WEBSITE_SEARCH_VIEWER)
    except flight_holds.HoldError as error:
        if error.status >= 500 or error.code in ("booking_off", "not_allowed", "no_selling_price"):
            logger.warning("Website flight hold not placed", extra={"code": error.code, "status": error.status})
            raise PublicError(502 if error.status >= 500 else 409, HOLD_NOT_PLACED, "hold_failed") from None
        raise PublicError(error.status, error.message, error.code) from None
    # D49: bank transfer and JazzCash never confirm or issue anything themselves. The booking stays held and staff are told
    # (in the admin and by email) to check the money arrived, record it, and only then issue.
    if method in ("bank_transfer", "jazzcash", "book_only"):
        lead = hold_request.passengers[0]
        if method == "book_only":
            note = ("Website flight. Customer chose Book Only (no payment yet). Contact them to arrange payment, record it in the Payments "
                    "tab, then issue. Nothing is issued automatically. Pay attention to the hold's time limit.")
        else:
            label = "bank transfer" if method == "bank_transfer" else "JazzCash"
            note = (f"Website flight. Customer chose {label}. Check the {label} payment has arrived, record it in the Payments tab, then "
                    "issue. Nothing is issued automatically. Pay attention to the hold's time limit.")
        await notifications.booking_event(
            "book_only_hold" if method == "book_only" else "bank_transfer_hold", reference=view.booking_id,
            passenger=f"{lead.title or ''} {lead.first_name} {lead.last_name}".strip(),
            route=None, fare_minor=view.selling_total.amount_minor, currency=view.selling_total.currency, pnr=view.pnr,
            hold_expires_at=view.time_limit, note=note)
    await customer_emails.send(view.booking_id, HOLD_EMAIL[method])
    return view.booking_id


#: The customer email sent when a website booking is held, by payment method.
HOLD_EMAIL = {"card": "booking_held", "book_only": "book_only_held", "bank_transfer": "bank_transfer", "jazzcash": "jazzcash"}


#: A website flight hold can be paid online only while this much of the hold is left: time for the PayFast checkout
#: plus the ticketing margin, so a paid booking can still be issued before the airline releases it.
FLIGHT_CHECKOUT_SECONDS = 15 * 60


def flight_can_pay(row: dict, now: datetime) -> bool:
    expires = row["hold_expires_at"]
    return (row["product"] == "flight" and row["booking_status"] == "held" and row["payment_status"] != "paid"
            and row["issuance_status"] == "not_issued" and bool(row["pnr"]) and card_allowed(row.get("provider_id")) and expires is not None
            and (expires - now).total_seconds() > FLIGHT_CHECKOUT_SECONDS + settings.ticketing_deadline_margin_seconds)


def flight_paid_status(row: dict) -> str:
    """Paid, not yet issued. 'processing': the gateway issues it (card, ticketing on) or is finishing it, so the guest sees
    "payment received, issuance in progress". 'confirmed': issuance is pending with Travelian's team (ticketing off or a
    supplier issued outside Travelian), "payment received, issuance pending"."""
    from app.services import issuance

    expires = row.get("hold_expires_at")
    if row["booking_status"] == "paid_not_ticketed" or (expires is not None and expires <= _now() and row["issuance_status"] != "unknown"):
        return "paid_not_ticketed"  # "Payment received. Our team will contact you shortly about your booking."
    if row["issuance_status"] in ("processing", "unknown"):
        return "processing"
    return "processing" if row["pnr"] and issuance.ticketing_on(row.get("provider_id")) else "confirmed"


async def _other_view(row: dict) -> PublicBookingView:
    """The guest's view of a non-group booking (website flights): no PNR or tickets until issued."""
    now = _now()
    status = lookup_status(row, now)
    view_status = {"requested": "requested", "issued": "issued", "expired": "expired", "cancelled": "released",
                   "paid": flight_paid_status(row)}.get(status, "held")
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
        live=row["booking_status"] != "requested", can_pay=view_status == "held" and flight_can_pay(row, now),
        total=PublicMoney(amount_minor=row["total_minor"], currency=row["currency"]),
        paid=PublicMoney(amount_minor=row["paid_minor"], currency=row["currency"]),
        hold_expires_at=row["hold_expires_at"] if view_status == "held" else None,
        airline=first.get("airline_name"), route=f"{first.get('origin')}-{first.get('destination')}" if first.get("origin") else None,
        legs=legs, travelers=[f"{t.title} {t.first_name} {t.last_name}".strip() for t in (people.travelers if people else [])],
        final_pnr=(row["final_pnr"] or row["pnr"]) if issued else None,
        tickets=[t.ticket_number for t in (people.travelers if people else []) if t.ticket_number] if issued else [],
    )


async def booking_view(reference: str) -> PublicBookingView:
    view, product = await _booking_view(reference)
    return await run_in_threadpool(with_payment_details, view, product)


def card_fee_for(due_minor: int, product: str, currency: str) -> CardFeeView | None:
    """The card fee on an amount due, from Finance settings (gateway-side only)."""
    from app.db import finance
    from app.services import card_fee

    if due_minor <= 0 or currency != "PKR":
        return None
    quote = card_fee.quote(due_minor, product, finance.get_settings())
    return CardFeeView(percent=quote.percent_label, fee=PublicMoney(amount_minor=quote.fee_minor, currency=currency),
                       total=PublicMoney(amount_minor=quote.total_minor, currency=currency))


def with_payment_details(view: PublicBookingView, product: str) -> PublicBookingView:
    from app.db import payment_slips

    if view.can_pay:
        view.card_fee = card_fee_for(view.total.amount_minor - view.paid.amount_minor, product, view.total.currency)
    view.chosen_account_id = payment_slips.chosen_account(view.reference)
    return view


async def _booking_view(reference: str) -> tuple[PublicBookingView, str]:
    row = await run_in_threadpool(public_bookings.summary, reference)
    if row is not None and row["product"] != "group":
        return await _other_view(row), row["product"]
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
    can_pay = (status == "held" and booking.payment_status != "paid" and card_allowed(None)
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
    ), "group"


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
    "paid_not_ticketed": "Payment received. Our team will contact you shortly about your booking.",
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
    return lookup_view(row)


async def lookup_by_link(reference: str) -> LookupView:
    """A booking opened with a valid signed email link (the link proved access): the same read-only view as Find my booking."""
    row = await run_in_threadpool(lambda: public_bookings.lookup(reference, "", trusted=True))
    if row is None:
        raise PublicError(404, "This link has expired or isn't valid. Find your booking with its reference and your email or phone.", "link_expired")
    return lookup_view(row)


def lookup_view(row: dict) -> LookupView:
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
        next_steps=NEXT_STEPS["paid_not_ticketed" if row["booking_status"] == "paid_not_ticketed" else status],
    )


# ---------- pay ----------

class CheckoutView(BaseModel):
    action: str = Field(description="PayFast's checkout URL: the browser posts `fields` there as a form.")
    fields: dict[str, str]
    sandbox: bool


async def start_payment(reference: str, token: str) -> CheckoutView:
    view = await open_booking(reference, token)
    if not payfast.payments_allowed():  # UAT PayFast only with PAYFAST_ALLOW_UAT (test money)
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
    # travelian#74: the card fee is added here, by the gateway, and PayFast is charged and verified against the total.
    fee = view.card_fee.fee.amount_minor if view.card_fee else 0
    charge = due + fee
    people = await run_in_threadpool(booking_people.read, reference)
    customer = people.customer if people else None
    basket_id = payfast.new_basket_id(reference)
    try:
        payfast_token = await payfast.access_token(basket_id, charge, "PKR")
    except payfast.PayFastError as error:
        raise PublicError(503, error.message, "payments_unavailable") from None
    await run_in_threadpool(lambda: public_bookings.create_attempt(reference, basket_id, charge, "PKR", card_fee_minor=fee))
    return_url = f"{settings.public_site_url}/my-bookings?ref={quote(reference)}"
    form = payfast.checkout_form(
        token=payfast_token, basket_id=basket_id, amount_minor=charge, currency="PKR",
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
    if outcome.status == "paid" and outcome.payment_status == "paid":
        # D48: the only way into automatic flight issuance. Reached only here: a result verified with the secured key, for
        # the amount asked, recorded once (a repeat is 'already_paid'), that made the booking fully paid.
        from app.services import flight_payments

        if outcome.product == "flight":
            flight_payments.start_after_verified_card_payment(outcome.reference)
    return outcome


# ---------- pay: Bank Alfalah (Alfa Payment Gateway) ----------

def alfalah_return_url() -> str:
    """Where APG sends the customer back: the website's own page, which hands the order id to the gateway."""
    return f"{settings.public_site_url}/payment/alfalah"


async def start_alfalah_payment(reference: str, token: str) -> CheckoutView:
    """The APG checkout form for the amount due (plus the card fee). The browser posts `fields` to `action`."""
    view = await open_booking(reference, token)
    if not alfalah.payments_allowed():  # sandbox APG only with ALFALAH_ALLOW_SANDBOX (test money)
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
    # travelian#74: the card fee is added here, by the gateway, and APG is charged and verified against the total.
    fee = view.card_fee.fee.amount_minor if view.card_fee else 0
    charge = due + fee
    order_id = alfalah.new_order_id(reference)
    return_url = alfalah_return_url()
    try:
        auth_token = await alfalah.handshake(order_id, return_url)
        form = alfalah.checkout_form(auth_token=auth_token, order_id=order_id, amount_minor=charge, return_url=return_url)
    except alfalah.AlfalahError as error:
        raise PublicError(503, error.message, "payments_unavailable") from None
    await run_in_threadpool(lambda: public_bookings.create_attempt(reference, order_id, charge, "PKR", card_fee_minor=fee,
                                                                   provider="alfalah", test_money=alfalah.sandbox()))
    return CheckoutView(action=form.action, fields=form.fields, sandbox=alfalah.sandbox())


async def alfalah_result(params: dict[str, str], source: str) -> public_bookings.Outcome | None:
    """Apply a Bank Alfalah result. The return and the listener call only name the order: the gateway asks APG itself for
    its status and records only "Paid" for this merchant and order, for the amount asked. None when nothing could be
    verified (no order id, APG unreachable, or not yet paid): nothing is changed, the customer can come back later."""
    if not alfalah.configured():
        return None
    order_id = alfalah.order_id_from(params)
    if order_id is None:
        return None
    result = await alfalah.order_status(order_id)
    if result is None:
        logger.warning("Bank Alfalah status could not be verified", extra={"source": source})
        return None
    if not result.paid:
        # Not paid (yet): APG's statuses besides "Paid" aren't a documented final failure, so the attempt stays open and a
        # later return or listener call can still record it. The customer sees that the payment wasn't completed.
        logger.info("Bank Alfalah order not paid", extra={"source": source, "status": result.status[:40]})
        attempt = await run_in_threadpool(public_bookings.attempt_by_basket, order_id)
        return public_bookings.Outcome(attempt.reference if attempt else "", "not_paid")
    outcome = await run_in_threadpool(
        lambda: public_bookings.record_result(order_id, paid=True, err_code="00", transaction_id=result.transaction_id,
                                              amount_minor=result.amount_minor, message=result.message, source=source, now=_now(),
                                              provider="alfalah", test_money=alfalah.sandbox()))
    logger.info("Bank Alfalah result applied", extra={"source": source, "outcome": outcome.status, "reference": outcome.reference})
    if outcome.status == "paid" and outcome.payment_status == "paid" and alfalah.production():
        # D48: automatic flight issuing only after a verified payment of real money (never on the APG sandbox).
        from app.services import flight_payments

        if outcome.product == "flight":
            flight_payments.start_after_verified_card_payment(outcome.reference)
    return outcome
