"""Live flight search on Southair with server-side sessions (spec v3.0 D23, D24).

A search keeps its NLD and fare hashes on the server for SEARCH_TTL; clients only get
opaque IDs bound to the user who searched. Selling price = supplier fare + configured
flight markup per passenger; without a markup no price is returned.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from app.core.config import settings
from app.core.field_visibility import BackOfficeUser
from app.schemas.flights import FlightSearchRequest
from app.schemas.live_flights import FareView, FlightMoney, FlightSearchView, FlightView, JourneyView, ProviderStatus
from app.vendors.flights.southair import normalize
from app.vendors.flights.southair.client import NAME, SouthairClient, SouthairError, SouthairNotConfigured, configured_client

logger = logging.getLogger(__name__)

SEARCH_TTL_SECONDS = 15 * 60
MAX_SESSIONS = 500
#: D24, for partial results once a second provider is added; never sent with no results.
MISSING_NOTICE = "Some results may be missing."


@dataclass(frozen=True)
class FareRef:
    direction: str
    route_key: str
    flight_key: str
    fare_code: str
    fare_hash: str
    adult_total: normalize.Amount | None


@dataclass
class SearchSession:
    id: str
    user_id: str
    request: FlightSearchRequest
    nld: str
    expires_at: float
    fares: dict[str, FareRef] = field(default_factory=dict)


_sessions: OrderedDict[str, SearchSession] = OrderedDict()
_account_currency: str | None = None


def _now() -> float:
    return time.time()


def _store(session: SearchSession) -> None:
    now = _now()
    for key in [key for key, value in _sessions.items() if value.expires_at <= now]:
        del _sessions[key]
    _sessions[session.id] = session
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


def get_session(search_id: str, user_id: str) -> SearchSession | None:
    session = _sessions.get(search_id)
    if session is None or session.user_id != user_id or session.expires_at <= _now():
        return None
    return session


def to_minor(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def sell(cost_minor: int) -> int | None:
    """Per passenger: cost + percent + fixed, rounded up to a whole unit. None without a markup."""
    percent, fixed = settings.flight_markup_percent, settings.flight_markup_fixed_minor
    if percent <= 0 and fixed <= 0:
        return None
    raw = Decimal(cost_minor) * (1 + percent / 100) + fixed
    return int((raw / 100).quantize(Decimal("1"), ROUND_CEILING) * 100)


async def account_currency(client: SouthairClient) -> str | None:
    """The agent account's currency (profile office.currency), used when a fare has none."""
    global _account_currency
    if _account_currency is None:
        try:
            office = (await client.profile()).get("office") or {}
        except SouthairError:
            return None
        currency = str(office.get("currency") or "").strip().upper()
        _account_currency = currency if len(currency) == 3 and currency.isalpha() else None
    return _account_currency


def fare_currency(total: normalize.Amount | None, fallback: str | None) -> str | None:
    return (total.currency if total and total.currency else fallback) if total else None


def _fare_view(fare_id: str, fare: normalize.Fare, viewer: BackOfficeUser, fallback_currency: str | None) -> FareView:
    view = FareView(id=fare_id, name=fare.code, meal=fare.meal, baggage=fare.baggage, refund=fare.refund, time_limit=fare.time_limit)
    currency = fare_currency(fare.adult_total, fallback_currency)
    if fare.adult_total and currency:
        cost = to_minor(fare.adult_total.value)
        selling = sell(cost)
        if selling is not None:
            view.selling_price = FlightMoney(amount_minor=selling, currency=currency)
        if viewer.visibility.cost_and_margin:
            view.supplier_cost = FlightMoney(amount_minor=cost, currency=currency)
    if viewer.visibility.supplier_names:
        view.supplier = NAME
    return view


async def search(request: FlightSearchRequest, viewer: BackOfficeUser, client_factory=None) -> FlightSearchView:
    session = SearchSession(id=secrets.token_urlsafe(16), user_id=viewer.user_id, request=request, nld="", expires_at=_now() + SEARCH_TTL_SECONDS)
    expires_at = datetime.fromtimestamp(session.expires_at, timezone.utc)
    providers = [ProviderStatus(name=NAME, status="failed")]
    try:
        client = (client_factory or configured_client)()
        data = await client.search(
            origin=request.origin, destination=request.destination, departure=request.departure_date,
            return_date=request.return_date, adults=request.adults, children=request.children, infants=request.infants,
        )
        nld, legs = normalize.legs(data)
        if not nld:
            raise SouthairError("invalid_response", "The flight supplier returned an unusable response.")
        currency = await account_currency(client) if any(f.adult_total and not f.adult_total.currency for leg in legs for fl in leg.flights for f in fl.fares) else None
    except SouthairNotConfigured:
        providers = [ProviderStatus(name=NAME, status="not_configured")]
        return _unavailable(session, expires_at, providers, viewer)
    except SouthairError as error:
        logger.warning("Flight search failed", extra={"error_code": error.error_code})
        return _unavailable(session, expires_at, providers, viewer)

    session.nld = nld
    journeys = []
    for leg in legs:
        direction = "outbound" if leg.origin == request.origin else "return"
        flights = []
        for flight in sorted(leg.flights, key=lambda f: f.departure_local or ""):
            fare_views = []
            for fare in flight.fares:
                fare_id = secrets.token_urlsafe(12)
                session.fares[fare_id] = FareRef(direction, leg.route_key, flight.key, fare.code, fare.fare_hash, fare.adult_total)
                fare_views.append(_fare_view(fare_id, fare, viewer, currency))
            flights.append(FlightView(
                id=secrets.token_urlsafe(9), airline_code=flight.airline_code, airline_name=flight.airline_name,
                flight_number=flight.flight_number, departure_local=flight.departure_local, arrival_local=flight.arrival_local,
                duration_minutes=flight.duration_minutes, stops=flight.stops, stop_cities=flight.stop_cities, fares=fare_views,
            ))
        journeys.append(JourneyView(direction=direction, origin=leg.origin, destination=leg.destination, flights=flights))
    _store(session)
    view = FlightSearchView(search_id=session.id, expires_at=expires_at, status="complete", journeys=journeys)
    if viewer.visibility.supplier_names:
        view.providers = [ProviderStatus(name=NAME, status="responded")]
    return view


def _unavailable(session: SearchSession, expires_at: datetime, providers: list[ProviderStatus], viewer: BackOfficeUser) -> FlightSearchView:
    # No results at all: not "some results may be missing" (D24 is for partial results).
    view = FlightSearchView(search_id=session.id, expires_at=expires_at, status="unavailable", journeys=[])
    if viewer.visibility.supplier_names:
        view.providers = providers
    return view


# ---------- reprice (step d) ----------

QUOTE_TTL_SECONDS = 10 * 60
_quotes: OrderedDict[str, "Quote"] = OrderedDict()


class RepriceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Quote:
    id: str
    user_id: str
    request: FlightSearchRequest
    nld: str
    refs: list[FareRef]
    currency: str
    supplier_total_minor: int
    selling_total_minor: int
    passengers: list[tuple[str, int, int, int]]  # (type, count, supplier_each_minor, selling_each_minor)
    expires_at: float


def get_quote(quote_id: str, user_id: str) -> Quote | None:
    quote = _quotes.get(quote_id)
    if quote is None or quote.user_id != user_id or quote.expires_at <= _now():
        return None
    return quote


def _store_quote(quote: Quote) -> None:
    now = _now()
    for key in [key for key, value in _quotes.items() if value.expires_at <= now]:
        del _quotes[key]
    _quotes[quote.id] = quote
    while len(_quotes) > MAX_SESSIONS:
        _quotes.popitem(last=False)


def _selected_refs(session: SearchSession, fare_ids: list[str]) -> list[FareRef]:
    refs = [session.fares.get(fare_id) for fare_id in fare_ids]
    if any(ref is None for ref in refs):
        raise RepriceError(404, "That fare isn't part of this search. Search again.")
    directions = [ref.direction for ref in refs]
    expected = ["outbound", "return"] if session.request.return_date else ["outbound"]
    if directions != expected:
        raise RepriceError(422, "Choose one outbound fare" + (" and one return fare." if session.request.return_date else "."))
    return refs  # type: ignore[return-value]


async def price_selection(request: FlightSearchRequest, wanted: list[FareRef], client: SouthairClient) -> tuple[str, list[FareRef], str, list[tuple[str, int, int]]]:
    """Repeat the search, find the same flights and fares, and price them.

    Returns (nld, fresh refs, currency, [(pax type, count, supplier cost each in minor units)]).
    Mixed passengers need explicit per-type totals from fare_info; adults alone can use
    the search's adult TOTAL. Anything else is refused rather than guessed.
    """
    data = await client.search(
        origin=request.origin, destination=request.destination, departure=request.departure_date,
        return_date=request.return_date, adults=request.adults, children=request.children, infants=request.infants,
    )
    nld, legs = normalize.legs(data)
    if not nld:
        raise RepriceError(502, "The flight supplier returned an unusable response. Search again.")
    fresh: list[FareRef] = []
    for ref in wanted:
        leg = next((leg for leg in legs if leg.route_key == ref.route_key), None)
        flight = next((f for f in (leg.flights if leg else []) if f.key == ref.flight_key), None)
        fare = next((f for f in (flight.fares if flight else []) if f.code == ref.fare_code), None)
        if fare is None:
            raise RepriceError(409, "This fare is no longer available. Search again and choose another fare.")
        fresh.append(FareRef(ref.direction, ref.route_key, ref.flight_key, fare.code, fare.fare_hash, fare.adult_total))

    fallback = await account_currency(client)
    counts = [("ADT", request.adults), ("CHD", request.children), ("INF", request.infants)]
    fare_info: dict | None = None
    try:
        fare_info = await client.fare_info(nld=nld, selections=[(r.route_key, r.fare_hash) for r in fresh])
    except SouthairError as error:
        logger.info("Fare info before booking unavailable", extra={"error_code": error.error_code})
    per_type = normalize.fare_info_totals(fare_info) if isinstance(fare_info, dict) else None
    final = normalize.fare_info_final_total(fare_info)
    if per_type and all(kind in per_type for kind, number in counts if number):
        currencies = {fare_currency(per_type[kind], fallback) for kind, number in counts if number}
        priced = [(kind, number, to_minor(per_type[kind].value)) for kind, number in counts if number]
    elif request.children == 0 and request.infants == 0 and all(r.adult_total for r in fresh):
        # Adults only: the search's per-adult totals, verified against fare_info's FINAL_TOTAL.
        # No verified total, no price: never confirm (or hold) on an unverified total.
        each = sum(to_minor(r.adult_total.value) for r in fresh)  # type: ignore[union-attr]
        if final is None:
            raise RepriceError(502, "The supplier didn't confirm the total for this fare, so it can't be confirmed. Try again.")
        if to_minor(final.value) != each * request.adults:
            raise RepriceError(409, "The supplier's total doesn't match this fare, so it can't be confirmed. Search again.")
        currencies = {fare_currency(r.adult_total, fallback) for r in fresh} | {fare_currency(final, fallback)}
        priced = [("ADT", request.adults, each)]
    else:
        raise RepriceError(422, "Child and infant fares can't be confirmed for this fare yet. Search for adults only.")
    if len(currencies) != 1 or None in currencies:
        raise RepriceError(502, "The supplier's fare currency is unclear. This fare can't be confirmed.")
    return nld, fresh, currencies.pop(), priced  # type: ignore[return-value]


def verified_total_minor(fare_info: dict | None, counts: dict[str, int]) -> int | None:
    """The supplier total fare_info confirms for this passenger mix, in minor units, or None.

    Explicit per-type TOTALs (guide shape) when every booked type has one; otherwise
    FINAL_TOTAL (live UAT). Never computed from fare components.
    """
    per_type = normalize.fare_info_totals(fare_info) if isinstance(fare_info, dict) else None
    if per_type and all(kind in per_type for kind, number in counts.items() if number):
        return sum(to_minor(per_type[kind].value) * number for kind, number in counts.items() if number)
    final = normalize.fare_info_final_total(fare_info)
    return to_minor(final.value) if final else None


def can_book(viewer: BackOfficeUser) -> bool:
    from app.core.field_visibility import BACK_OFFICE_ROLES

    return settings.southair_booking_enabled and viewer.role in BACK_OFFICE_ROLES


async def reprice(search_id: str, fare_ids: list[str], viewer: BackOfficeUser, client_factory=None) -> "RepriceView":
    from app.schemas.live_flights import PassengerPrice, RepriceView

    session = get_session(search_id, viewer.user_id)
    if session is None:
        raise RepriceError(404, "This search has expired. Search again.")
    wanted = _selected_refs(session, fare_ids)
    try:
        client = (client_factory or configured_client)()
        nld, fresh, currency, priced = await price_selection(session.request, wanted, client)
    except SouthairError as error:
        logger.warning("Reprice failed", extra={"error_code": error.error_code})
        raise RepriceError(502, "The flight supplier couldn't reprice this fare. Try again.") from None
    passengers = []
    for kind, number, cost_each in priced:
        selling_each = sell(cost_each)
        if selling_each is None:
            raise RepriceError(409, "No selling price is configured for flights, so this fare can't be confirmed.")
        passengers.append((kind, number, cost_each, selling_each))
    supplier_total = sum(number * cost for _, number, cost, _ in passengers)
    selling_total = sum(number * selling for _, number, _, selling in passengers)
    quote = Quote(
        id=secrets.token_urlsafe(16), user_id=viewer.user_id, request=session.request, nld=nld, refs=fresh, currency=currency,
        supplier_total_minor=supplier_total, selling_total_minor=selling_total, passengers=passengers,
        expires_at=_now() + QUOTE_TTL_SECONDS,
    )
    _store_quote(quote)
    old_adult = sum(to_minor(r.adult_total.value) for r in wanted if r.adult_total)
    new_adult = sum(to_minor(r.adult_total.value) for r in fresh if r.adult_total)
    view = RepriceView(
        quote_id=quote.id, expires_at=datetime.fromtimestamp(quote.expires_at, timezone.utc),
        selling_total=FlightMoney(amount_minor=selling_total, currency=currency),
        passengers=[PassengerPrice(type=kind, count=number, selling_each=FlightMoney(amount_minor=selling, currency=currency)) for kind, number, _, selling in passengers],
        price_changed=old_adult != new_adult, booking_enabled=can_book(viewer),
    )
    if viewer.visibility.cost_and_margin:
        view.supplier_total = FlightMoney(amount_minor=supplier_total, currency=currency)
    return view
