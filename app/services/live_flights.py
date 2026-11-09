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
    view = FlightSearchView(search_id=session.id, expires_at=expires_at, status="unavailable", notice=MISSING_NOTICE, journeys=[])
    if viewer.visibility.supplier_names:
        view.providers = providers
    return view
