"""Live flight search on Southair with server-side sessions (spec v3.0 D23, D24).

A search keeps its NLD and fare hashes on the server for SEARCH_TTL; clients only get
opaque IDs bound to the user who searched. Selling price = supplier fare + configured
flight markup per passenger; without a markup no price is returned.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from app.core.config import settings
from app.core.field_visibility import BackOfficeUser
from app.schemas.flights import FlightSearchRequest
from app.schemas.live_flights import FareView, FlightMoney, PassengerPrice, FlightSearchView, FlightView, JourneyView, ProviderStatus, SegmentView
from app.vendors.flights.southair import normalize
from app.vendors.flights.southair.client import NAME, SouthairClient, SouthairError, SouthairNotConfigured, configured_client
from app.services import baggage, restricted_airlines
from app.vendors.flights.sooperfare import client as sooperfare
from app.vendors.flights.sooperfare import normalize as sf_normalize
from app.vendors.flights.kyte import client as kyte
from app.vendors.flights.kyte import normalize as ky_normalize

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
    #: The flight as shown (airline, number, times, fare, baggage): saved as the booking's itinerary.
    segment: dict | None = None
    #: "southair" or "sooperfare". SooperFare fares carry `sf`: its airline (provider id), flight and fare signatures.
    provider: str = "southair"
    sf: "SfRef | None" = None
    #: Kyte fares carry `ky`: the offer (one per direction), the shopping flow's transaction id and the search prices.
    ky: "KyRef | None" = None


@dataclass(frozen=True)
class SfRef:
    provider_id: str  # SooperFare's airline identifier: PA, PF, PK, ONEAPI, ...
    flight_signature: str
    fare_signature: str


@dataclass(frozen=True)
class KyRef:
    offer_id: str
    transaction_id: str
    currency: str
    each: tuple[tuple[str, int], ...]  # supplier price per passenger type at search, minor units
    total_minor: int


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


def money_text(amount_minor: int, currency: str) -> str:
    """User-facing amount: "Rs 13,997" for PKR (AGENTS.md), otherwise the currency code."""
    label = "Rs" if currency.upper() == "PKR" else currency.upper()
    whole, paisa = divmod(amount_minor, 100)
    return f"{label} {whole:,}" + (f".{paisa:02d}" if paisa else "")


def to_minor(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def sell(cost_minor: int) -> int | None:
    """Per passenger: the supplier cost + FLIGHT_MARKUP_PERCENT + FLIGHT_MARKUP_FIXED_MINOR, rounded up to a whole rupee.
    No markup configured: the supplier cost itself (0% markup, the same rule as group fares). Never a multiplier in code;
    Pricing & Revenue's rules are not wired in yet (docs/investigations/2026-11-19-admin-flows.md, A3)."""
    percent = max(settings.flight_markup_percent, Decimal(0))
    fixed = max(settings.flight_markup_fixed_minor, 0)
    raw = Decimal(cost_minor) * (1 + percent / 100) + fixed
    return int((raw / 100).quantize(Decimal("1"), ROUND_CEILING) * 100)


def fee_share(fee_minor: int, adults: int) -> int:
    """SooperFare's per-booking platform fee, passed on per adult: split across the adults and rounded up to a whole
    rupee, so adults x share always covers the fee."""
    if fee_minor <= 0:
        return 0
    return int((Decimal(fee_minor) / max(adults, 1) / 100).quantize(Decimal("1"), ROUND_CEILING) * 100)


def sf_search_share(adults: int) -> int:
    """The fee share used for search prices (the real fee comes with SooperFare's quote)."""
    return fee_share(max(settings.sooperfare_platform_fee_minor, 0), adults)


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


def passenger_counts(request: FlightSearchRequest) -> list[tuple[str, int]]:
    """The searched travelers per type; a type with nobody is left out."""
    return [(kind, number) for kind, number in (("ADT", request.adults), ("CHD", request.children), ("INF", request.infants)) if number]


def selling_by_type(counts: list[tuple[str, int]], cost_each: dict[str, int], adult_extra: int = 0) -> list[tuple[str, int, int]] | None:
    """[(type, count, selling each)] for the searched travelers: each type's own supplier price per passenger with the
    markup (the same rule as reprice); adult_extra is SooperFare's platform fee share per adult. None when any searched
    type has no supplier price or no selling price (never guessed from the adult's price)."""
    rows = []
    for kind, number in counts:
        cost = cost_each.get(kind)
        selling = sell(cost) if cost is not None else None
        if selling is None:
            return None
        rows.append((kind, number, selling + (adult_extra if kind == "ADT" else 0)))
    return rows or None


def _set_total(view: FareView, rows: list[tuple[str, int, int]] | None, currency: str) -> None:
    """The fare's total for all travelers (travelian#113), so no page multiplies a per-adult price."""
    if not rows:
        return
    view.total_price = FlightMoney(amount_minor=sum(number * each for _, number, each in rows), currency=currency)
    view.passenger_prices = [PassengerPrice(type=kind, count=number, selling_each=FlightMoney(amount_minor=each, currency=currency))  # type: ignore[arg-type]
                             for kind, number, each in rows]


def southair_cost_each(fare: normalize.Fare, currency: str) -> dict[str, int]:
    """Southair's supplier price per passenger type: the adult's search total (live: ACC, the same for any mix), and a
    child's or infant's only when the fare sent its own TOTAL in the same currency."""
    costs = {"ADT": to_minor(fare.adult_total.value)} if fare.adult_total else {}
    for kind, amount in fare.other_totals.items():
        if (amount.currency or currency) == currency:
            costs[kind] = to_minor(amount.value)
    return costs


def sooperfare_cost_each(fare: sf_normalize.Fare, counts: list[tuple[str, int]]) -> dict[str, int]:
    """SooperFare prices the searched mix: per type, its total for that many travelers. A type is used only when its
    count is the one searched and the total splits evenly per passenger (as reprice requires)."""
    costs = {}
    for kind, number in counts:
        count, total = fare.per_type.get(kind, (0, 0))
        if count == number and total % number == 0:
            costs[kind] = total // number
    return costs


def _fare_view(fare_id: str, fare: normalize.Fare, viewer: BackOfficeUser, fallback_currency: str | None,
               counts: list[tuple[str, int]] | None = None) -> FareView:
    view = FareView(id=fare_id, name=fare.code, meal=fare.meal, baggage=fare.baggage.short(), baggage_detail=fare.baggage.detail(),
                    refund=fare.refund, time_limit=fare.time_limit)
    currency = fare_currency(fare.adult_total, fallback_currency)
    if fare.adult_total and currency:
        cost = to_minor(fare.adult_total.value)
        selling = sell(cost)
        if selling is not None:
            view.selling_price = FlightMoney(amount_minor=selling, currency=currency)
            _set_total(view, selling_by_type(counts or [("ADT", 1)], southair_cost_each(fare, currency)), currency)
        if viewer.visibility.cost_and_margin:
            view.supplier_cost = FlightMoney(amount_minor=cost, currency=currency)
    if viewer.visibility.supplier_names:
        view.supplier = NAME
    return view


def _gap_minutes(arrived: str | None, departs: str | None) -> int | None:
    """Minutes between two local times at the same airport ("YYYY-MM-DDTHH:MM"); None when either is missing or out of order."""
    try:
        minutes = int((datetime.fromisoformat(departs) - datetime.fromisoformat(arrived)).total_seconds() // 60)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return minutes if minutes >= 0 else None


#: A flight number's airline: two letters/digits, at least one a letter, then the digits ("TK8050", "9P 842").
_FLIGHT_PREFIX = re.compile(r"^\s*([A-Z0-9]{2})[\s-]*\d", re.IGNORECASE)


def _airline_code(value) -> str | None:
    if isinstance(value, re.Match):
        value = value.group(1)
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"[A-Z0-9]{2,3}", text) and re.search(r"[A-Z]", text) else None


def _name(value) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:80] or None


def segment_views(segments) -> list[SegmentView]:
    """A flight's connection, flight by flight, with the time on the ground at each stop (#37, #83, #84, #119, #122).
    Every segment must name both airports, else none are returned (the stop list alone is then all there is)."""
    rows = [dict(row) for row in segments or []]
    if not rows or not all(row.get("origin") and row.get("destination") for row in rows):
        return []
    views = []
    for index, row in enumerate(rows):
        layover = _gap_minutes(rows[index - 1].get("arrival_local"), row.get("departure_local")) if index else None
        minutes = row.get("duration_minutes")
        code = _airline_code(row.get("airline_code")) or _airline_code(_FLIGHT_PREFIX.match(str(row.get("flight_number") or "")))
        operating = _airline_code(row.get("operating"))
        codeshare = bool(operating and operating != code)
        views.append(SegmentView(
            flight_number=row.get("flight_number") or None, airline_code=code, airline_name=_name(row.get("airline_name")),
            operating_code=operating if codeshare else None, operating_name=_name(row.get("operating_name")) if codeshare else None,
            origin=str(row["origin"]).upper(), destination=str(row["destination"]).upper(),
            departure_local=row.get("departure_local"), arrival_local=row.get("arrival_local"),
            duration_minutes=minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0 else None,
            layover_minutes=layover,
        ))
    return views


def segment(direction: str, leg: normalize.Leg, flight: normalize.Flight, fare: normalize.Fare) -> dict:
    """One flight of a booking's itinerary, as the supplier described it. No passenger data."""
    return {
        "direction": direction, "origin": leg.origin, "destination": leg.destination,
        "airline_code": flight.airline_code, "airline_name": flight.airline_name, "flight_number": flight.flight_number,
        "departure_local": flight.departure_local, "arrival_local": flight.arrival_local,
        "duration_minutes": flight.duration_minutes, "stops": flight.stops, "stop_cities": list(flight.stop_cities),
        "fare_name": fare.code, "baggage": fare.baggage.short(), "baggage_parts": baggage.to_dict(fare.baggage), "meal": fare.meal,
    }


SOOPERFARE = "sooperfare"
#: The whole SooperFare part of a search (availability, then every airline in parallel) stops waiting after this.
SOOPERFARE_SEARCH_SECONDS = 25


def _search_args(request: FlightSearchRequest) -> dict:
    return dict(origin=request.origin, destination=request.destination, departure=request.departure_date,
                return_date=request.return_date, adults=request.adults, children=request.children, infants=request.infants,
                cabin=request.cabin)


async def _southair_part(request: FlightSearchRequest, client_factory):
    client = (client_factory or configured_client)()
    data = await client.search(
        origin=request.origin, destination=request.destination, departure=request.departure_date,
        return_date=request.return_date, adults=request.adults, children=request.children, infants=request.infants,
    )
    nld, legs = normalize.legs(data)
    if not nld:
        raise SouthairError("invalid_response", "The flight supplier returned an unusable response.")
    currency = await account_currency(client) if any(f.adult_total and not f.adult_total.currency for leg in legs for fl in leg.flights for f in fl.fares) else None
    return nld, legs, currency


#: SooperFare's list of airlines that fly a route changes rarely: kept per route for AIRLINES_TTL_SECONDS so the airline
#: searches start at once instead of after an availability call (~0.8 s). Fares are never cached here.
AIRLINES_TTL_SECONDS = 6 * 3600
_airlines: dict[tuple, tuple[float, list[str]]] = {}


async def _airlines_for(client, request: FlightSearchRequest, args: dict) -> list[str]:
    key = (request.origin, request.destination, request.return_date is not None, request.cabin)
    cached = _airlines.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    ids = await client.availability(**args)
    if ids:
        _airlines[key] = (time.monotonic() + AIRLINES_TTL_SECONDS, ids)
    return ids


async def sooperfare_results(request: FlightSearchRequest, factory=None, provider_ids: list[str] | None = None) -> list[sf_normalize.Result]:
    """SooperFare: the airlines it can search for this trip, then each of them in parallel. An airline that fails is
    left out (logged); raises SooperfareError only when no airline answered."""
    client = (factory or sooperfare.SooperfareClient)()
    args = _search_args(request)
    ids = provider_ids if provider_ids is not None else await _airlines_for(client, request, args)
    answers = await asyncio.gather(*(client.search(provider=pid, **args) for pid in ids), return_exceptions=True)
    results, failures = [], 0
    for pid, answer in zip(ids, answers):
        if isinstance(answer, BaseException):
            failures += 1
            logger.warning("SooperFare airline search failed", extra={"airline": pid, "error_code": getattr(answer, "error_code", type(answer).__name__)})
            continue
        results.extend(sf_normalize.parse_search(answer, request.origin, two_way=request.return_date is not None))
    if ids and failures == len(ids):
        raise sooperfare.SooperfareError("all_failed", "The flight supplier couldn't search right now.")
    return results


def _sf_segment(direction: str, flight: sf_normalize.Flight, fare: sf_normalize.Fare) -> dict:
    return {
        "direction": direction, "origin": flight.origin, "destination": flight.destination,
        "airline_code": flight.airline_code, "airline_name": flight.airline_name, "flight_number": flight.flight_number,
        "departure_local": flight.departure_local, "arrival_local": flight.arrival_local,
        "duration_minutes": flight.duration_minutes, "stops": flight.stops, "stop_cities": list(flight.stop_cities),
        "fare_name": fare.name, "baggage": fare.baggage.short(), "baggage_parts": baggage.to_dict(fare.baggage), "meal": None,
        "operating_codes": sorted({s["operating"] for s in flight.segments if s.get("operating")}),
    }


def _sf_fare_view(fare_id: str, fare: sf_normalize.Fare, viewer: BackOfficeUser, adults: int = 1,
                  counts: list[tuple[str, int]] | None = None) -> FareView:
    view = FareView(id=fare_id, name=fare.name, baggage=fare.baggage.short(), baggage_detail=fare.baggage.detail(), refund=fare.refund)
    each = fare.adult_each_minor
    if each is not None:
        selling = sell(each)
        if selling is not None:
            view.selling_price = FlightMoney(amount_minor=selling + sf_search_share(adults), currency=fare.currency)
            counts = counts or [("ADT", adults)]
            _set_total(view, selling_by_type(counts, sooperfare_cost_each(fare, counts), sf_search_share(adults)), fare.currency)
        if viewer.visibility.cost_and_margin:
            view.supplier_cost = FlightMoney(amount_minor=each, currency=fare.currency)
    if viewer.visibility.supplier_names:
        view.supplier = sooperfare.NAME
    return view


def _add_sooperfare(session: SearchSession, results: list[sf_normalize.Result], viewer: BackOfficeUser,
                    flights_by_direction: dict[str, list[tuple[str, FlightView]]]) -> None:
    """Each distinct SooperFare flight once per direction, with its fares. A return selection is matched to a bookable
    option (outbound + return of the same airline) when it is priced."""
    seen: set[tuple[str, str, str]] = set()
    for result in results:
        for option in result.options:
            for index, flight in enumerate(option.flights):
                direction = "outbound" if index == 0 else "return"
                key = (result.provider, direction, flight.signature)
                if key in seen:
                    continue
                seen.add(key)
                fare_views = []
                for fare in flight.fares:
                    fare_id = secrets.token_urlsafe(12)
                    each = fare.adult_each_minor
                    session.fares[fare_id] = FareRef(
                        direction, f"{flight.origin}{flight.destination}", flight.signature, fare.name, "",
                        normalize.Amount(Decimal(each) / 100, fare.currency) if each is not None else None,
                        _sf_segment(direction, flight, fare), provider=SOOPERFARE,
                        sf=SfRef(result.provider, flight.signature, fare.signature),
                    )
                    fare_views.append(_sf_fare_view(fare_id, fare, viewer, session.request.adults, passenger_counts(session.request)))
                flights_by_direction[direction].append((flight.departure_local or "", FlightView(
                    id=secrets.token_urlsafe(9), airline_code=flight.airline_code, airline_name=flight.airline_name,
                    flight_number=flight.flight_number, departure_local=flight.departure_local, arrival_local=flight.arrival_local,
                    duration_minutes=flight.duration_minutes, stops=flight.stops, stop_cities=flight.stop_cities,
                    segments=segment_views(flight.segments), fares=fare_views,
                )))


KYTE = kyte.NAME
#: The Kyte part of a search stops waiting after this (it fans out to every airline it searches).
KYTE_SEARCH_SECONDS = 25
KYTE_CABINS = {"economy": "economy", "premium_economy": "premiumEconomy", "business": "business", "first": "first"}
#: Kyte needs an age for children and infants (else they're priced as adults): a typical age per type.
KYTE_AGES = {"CHD": 8, "INF": 1}


def kyte_shop_body(request: FlightSearchRequest) -> dict:
    journeys = [{"departureAirport": request.origin, "arrivalAirport": request.destination, "date": {"main": request.departure_date.isoformat()}}]
    if request.return_date:
        journeys.append({"departureAirport": request.destination, "arrivalAirport": request.origin, "date": {"main": request.return_date.isoformat()}})
    passengers = ([{} for _ in range(request.adults)] + [{"age": KYTE_AGES["CHD"]} for _ in range(request.children)]
                  + [{"age": KYTE_AGES["INF"]} for _ in range(request.infants)])
    return {"journeys": journeys, "passengers": passengers, "cabinType": KYTE_CABINS.get(request.cabin, "economy"),
            "flexibility": "lowest", "fareType": "public", "exactMatch": False, "nonStopFlight": False,
            # Return trips: one offer per direction, so each fits the outbound / return lists (split offers).
            "splitOffer": request.return_date is not None, "originCurrency": False}


async def kyte_results(request: FlightSearchRequest, factory=None) -> tuple[list[ky_normalize.Flight], str]:
    """Kyte's flights for this search and the shopping flow's transaction id (kept for the offer-details call)."""
    client = (factory or kyte.configured_client)()
    transaction_id = kyte.new_transaction_id()
    data = await client.shop(kyte_shop_body(request), transaction_id=transaction_id)
    errors = ky_normalize.search_errors(data)
    if errors:
        logger.info("Kyte search answered with errors", extra={"codes": errors[:5]})
    return ky_normalize.parse_shop(data, request.origin, request.destination, two_way=request.return_date is not None), transaction_id


def _ky_segment(flight: ky_normalize.Flight, fare: ky_normalize.Fare) -> dict:
    return {
        "direction": flight.direction, "origin": flight.origin, "destination": flight.destination,
        "airline_code": flight.airline_code, "airline_name": flight.airline_name, "flight_number": flight.flight_number,
        "departure_local": flight.departure_local, "arrival_local": flight.arrival_local,
        "duration_minutes": flight.duration_minutes, "stops": flight.stops, "stop_cities": list(flight.stop_cities),
        "fare_name": fare.name, "baggage": fare.baggage.short(), "baggage_parts": baggage.to_dict(fare.baggage), "meal": None,
        "operating_codes": sorted({s["operating"] for s in flight.segments if s.get("operating")}),
    }


def _ky_fare_view(fare_id: str, fare: ky_normalize.Fare, viewer: BackOfficeUser, counts: list[tuple[str, int]]) -> FareView:
    # Kyte fares are requests until Kyte booking is wired in: no supplier hold, staff confirm and book.
    view = FareView(id=fare_id, name=fare.name, baggage=fare.baggage.short(), baggage_detail=fare.baggage.detail(), booking_mode="request")
    each = fare.adult_each_minor
    if each is not None:
        selling = sell(each)
        if selling is not None:
            view.selling_price = FlightMoney(amount_minor=selling, currency=fare.currency)
            if all(kind in fare.each for kind, number in counts if number):
                _set_total(view, selling_by_type(counts, dict(fare.each)), fare.currency)
        if viewer.visibility.cost_and_margin:
            view.supplier_cost = FlightMoney(amount_minor=each, currency=fare.currency)
    if viewer.visibility.supplier_names:
        view.supplier = kyte.NAME
    return view


def _add_kyte(session: SearchSession, flights: list[ky_normalize.Flight], transaction_id: str, viewer: BackOfficeUser,
              flights_by_direction: dict[str, list[tuple[str, FlightView]]]) -> None:
    counts = passenger_counts(session.request)
    for flight in flights:
        fare_views = []
        for fare in flight.fares:
            fare_id = secrets.token_urlsafe(12)
            each = fare.adult_each_minor
            session.fares[fare_id] = FareRef(
                flight.direction, f"{flight.origin}{flight.destination}", flight.signature, fare.name, "",
                normalize.Amount(Decimal(each) / 100, fare.currency) if each is not None else None,
                _ky_segment(flight, fare), provider=KYTE,
                ky=KyRef(fare.offer_id, transaction_id, fare.currency, tuple(sorted(fare.each.items())), fare.total_minor),
            )
            fare_views.append(_ky_fare_view(fare_id, fare, viewer, counts))
        flights_by_direction[flight.direction].append((flight.departure_local or "", FlightView(
            id=secrets.token_urlsafe(9), airline_code=flight.airline_code, airline_name=flight.airline_name,
            flight_number=flight.flight_number, departure_local=flight.departure_local, arrival_local=flight.arrival_local,
            duration_minutes=flight.duration_minutes, stops=flight.stops, stop_cities=flight.stop_cities,
            segments=segment_views(flight.segments), fares=fare_views,
        )))


async def search(request: FlightSearchRequest, viewer: BackOfficeUser, client_factory=None, sooperfare_factory=None,
                 kyte_factory=None) -> FlightSearchView:
    """Southair and SooperFare in parallel (D24): results merged by direction; a provider that fails doesn't hide the
    other's results ("Some results may be missing" then). SooperFare is searched only when configured."""
    session = SearchSession(id=secrets.token_urlsafe(16), user_id=viewer.user_id, request=request, nld="", expires_at=_now() + SEARCH_TTL_SECONDS)
    expires_at = datetime.fromtimestamp(session.expires_at, timezone.utc)
    use_sooperfare = sooperfare.configured() or sooperfare_factory is not None
    tasks = [_southair_part(request, client_factory)]
    if use_sooperfare:
        tasks.append(asyncio.wait_for(sooperfare_results(request, sooperfare_factory), SOOPERFARE_SEARCH_SECONDS))
    # Website searches see Kyte only with KYTE_PUBLIC_ENABLED (sandbox fares are never shown to customers).
    use_kyte = kyte_factory is not None or (kyte.configured() and (viewer.role != "website" or settings.kyte_public_enabled))
    if use_kyte:
        tasks.append(asyncio.wait_for(kyte_results(request, kyte_factory), KYTE_SEARCH_SECONDS))
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    providers: list[ProviderStatus] = []
    flights_by_direction: dict[str, list[tuple[str, FlightView]]] = {"outbound": [], "return": []}
    southair = outcomes[0]
    if isinstance(southair, SouthairNotConfigured):
        providers.append(ProviderStatus(name=NAME, status="not_configured"))
    elif isinstance(southair, BaseException):
        logger.warning("Flight search failed", extra={"error_code": getattr(southair, "error_code", type(southair).__name__)})
        providers.append(ProviderStatus(name=NAME, status="failed"))
    else:
        nld, legs, currency = southair
        session.nld = nld
        for leg in legs:
            direction = "outbound" if leg.origin == request.origin else "return"
            for flight in leg.flights:
                fare_views = []
                for fare in flight.fares:
                    fare_id = secrets.token_urlsafe(12)
                    session.fares[fare_id] = FareRef(direction, leg.route_key, flight.key, fare.code, fare.fare_hash, fare.adult_total,
                                                     segment(direction, leg, flight, fare))
                    fare_views.append(_fare_view(fare_id, fare, viewer, currency, passenger_counts(request)))
                flights_by_direction[direction].append((flight.departure_local or "", FlightView(
                    id=secrets.token_urlsafe(9), airline_code=flight.airline_code, airline_name=flight.airline_name,
                    flight_number=flight.flight_number, departure_local=flight.departure_local, arrival_local=flight.arrival_local,
                    duration_minutes=flight.duration_minutes, stops=flight.stops, stop_cities=flight.stop_cities,
                    segments=segment_views(flight.segments), fares=fare_views,
                )))
        providers.append(ProviderStatus(name=NAME, status="responded"))
    if use_sooperfare:
        sf = outcomes[1]
        if isinstance(sf, BaseException):
            logger.warning("SooperFare search failed", extra={"error_code": getattr(sf, "error_code", type(sf).__name__)})
            providers.append(ProviderStatus(name=sooperfare.NAME, status="failed"))
        else:
            _add_sooperfare(session, sf, viewer, flights_by_direction)
            providers.append(ProviderStatus(name=sooperfare.NAME, status="responded"))
    if use_kyte:
        ky = outcomes[-1]
        if isinstance(ky, BaseException):
            logger.warning("Kyte search failed", extra={"error_code": getattr(ky, "error_code", type(ky).__name__)})
            providers.append(ProviderStatus(name=kyte.NAME, status="failed"))
        else:
            ky_flights, transaction_id = ky
            _add_kyte(session, ky_flights, transaction_id, viewer, flights_by_direction)
            providers.append(ProviderStatus(name=kyte.NAME, status="responded"))

    for _, flight_view in (item for items in flights_by_direction.values() for item in items):
        for fare_view in flight_view.fares:
            ref = session.fares.get(fare_view.id)
            if ref is not None and restricted_airlines.segment_restricted(ref.segment):
                fare_view.booking_mode = "request"
    if not any(flights_by_direction.values()) and not any(p.status == "responded" for p in providers):
        return _unavailable(session, expires_at, providers, viewer)
    journeys = []
    for direction, origin, destination in (("outbound", request.origin, request.destination), ("return", request.destination, request.origin)):
        if direction == "return" and not request.return_date:
            continue
        flights = [view for _, view in sorted(flights_by_direction[direction], key=lambda item: item[0])]
        journeys.append(JourneyView(direction=direction, origin=origin, destination=destination, flights=flights))
    _store(session)
    partial = any(p.status == "failed" for p in providers) and any(p.status == "responded" for p in providers)
    view = FlightSearchView(search_id=session.id, expires_at=expires_at, status="complete", journeys=journeys,
                            notice=MISSING_NOTICE if partial else None)
    if viewer.visibility.supplier_names:
        view.providers = providers
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
    """code: stable reason for clients (fare_gone, search_expired, adults_only, no_selling_price,
    not_confirmed, change_too_large, invalid_selection); message: the sentence to show."""

    def __init__(self, status: int, message: str, code: str = "not_confirmed"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


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
    price_changed: bool = False
    old_total_minor: int | None = None  # selling total at the search prices, when known
    provider: str = "southair"
    #: SooperFare: what its reservation needs (search and option refs, passenger slots, platform fee). Server side only.
    supplier_context: dict | None = None


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
        raise RepriceError(404, "That fare isn't part of this search. Search again.", code="invalid_selection")
    directions = [ref.direction for ref in refs]
    expected = ["outbound", "return"] if session.request.return_date else ["outbound"]
    if directions != expected:
        raise RepriceError(422, "Choose one outbound fare" + (" and one return fare." if session.request.return_date else "."), code="invalid_selection")
    if len({ref.provider for ref in refs}) != 1 or len({ref.sf.provider_id for ref in refs if ref.sf}) > 1:  # type: ignore[union-attr]
        raise RepriceError(422, "Choose the outbound and return fares from the same airline.", code="invalid_selection")
    return refs  # type: ignore[return-value]


async def sf_price_selection(request: FlightSearchRequest, wanted: list[FareRef], factory=None) -> tuple[list[FareRef], str, list[tuple[str, int, int]], dict]:
    """SooperFare: repeat the search for that airline, find the same flights (one bookable option holding all of them)
    and fares, and quote them. Returns (fresh refs, currency, [(pax type, count, supplier cost each)], reservation context).
    Each type's total must split evenly per passenger; otherwise the fare is refused rather than guessed."""
    client = (factory or sooperfare.SooperfareClient)()
    provider_id = wanted[0].sf.provider_id  # type: ignore[union-attr]
    results = await sooperfare_results(request, lambda: client, provider_ids=[provider_id])
    signatures = [ref.sf.flight_signature for ref in wanted]  # type: ignore[union-attr]
    for result in results:
        for option in result.options:
            if [flight.signature for flight in option.flights] != signatures:
                continue
            fares = [next((fare for fare in flight.fares if fare.signature == ref.sf.fare_signature), None)  # type: ignore[union-attr]
                     for flight, ref in zip(option.flights, wanted)]
            if any(fare is None for fare in fares):
                continue
            answer = await client.quote(result.search_ref_id, [(option.ref_id, flight.ref_id, fare.ref_id)  # type: ignore[union-attr]
                                                               for flight, fare in zip(option.flights, fares)])
            quote = sf_normalize.parse_quote(answer)
            if quote is None:
                raise RepriceError(502, "The supplier's price couldn't be read. Search again.")
            if quote.fare_signatures != [fare.signature for fare in fares]:  # type: ignore[union-attr]
                raise RepriceError(409, "This fare is no longer available. Search again and choose another fare.", code="fare_gone")
            counts = [("ADT", request.adults), ("CHD", request.children), ("INF", request.infants)]
            priced = []
            for kind, number in counts:
                if not number:
                    continue
                count, total = quote.per_type.get(kind, (0, 0))
                if count != number or total % number:
                    raise RepriceError(422, "This fare can't be priced for these passengers. Search again.", code="adults_only")
                priced.append((kind, number, total // number))
            fresh = [replace(ref, adult_total=normalize.Amount(Decimal(fare.adult_each_minor) / 100, fare.currency)  # type: ignore[union-attr]
                             if fare.adult_each_minor is not None else None,  # type: ignore[union-attr]
                             segment=_sf_segment(ref.direction, flight, fare))  # type: ignore[arg-type]
                     for ref, flight, fare in zip(wanted, option.flights, fares)]
            context = {"provider_id": provider_id, "search_ref_id": result.search_ref_id, "option_ref_id": option.ref_id,
                       "passengers": [{"ref_id": p.ref_id, "type": p.kind, "required_fields": list(p.required_fields)} for p in quote.passengers],
                       "passport_required": quote.passport_required, "platform_fee_minor": quote.platform_fee_minor}
            return fresh, quote.currency, priced, context
    raise RepriceError(409, "This fare is no longer available. Search again and choose another fare.", code="fare_gone")


async def ky_price_selection(request: FlightSearchRequest, wanted: list[FareRef], factory=None) -> tuple[list[FareRef], str, list[tuple[str, int, int]], dict]:
    """Kyte: Offer Details for the chosen offers (one per direction) confirms each offer's current total. The per-type
    prices from the search are used only when the offer totals still match them exactly; a changed price is refused
    (search again) rather than split by guesswork."""
    client = (factory or kyte.configured_client)()
    refs = [ref.ky for ref in wanted]
    answer = await client.offer_details([ref.offer_id for ref in refs], transaction_id=refs[0].transaction_id)  # type: ignore[union-attr]
    currencies = set()
    for ref in refs:
        current = ky_normalize.offer_details_total(answer, ref.offer_id)  # type: ignore[union-attr]
        if current is None:
            raise RepriceError(409, "This fare is no longer available. Search again and choose another fare.", code="fare_gone")
        total, currency = current
        if total != ref.total_minor or currency != ref.currency:  # type: ignore[union-attr]
            raise RepriceError(409, "The airline changed this fare's price. Search again to see the new price.", code="fare_gone")
        currencies.add(currency)
    if len(currencies) != 1:
        raise RepriceError(502, "The supplier's fare currency is unclear. This fare can't be confirmed.")
    counts = [("ADT", request.adults), ("CHD", request.children), ("INF", request.infants)]
    priced = []
    for kind, number in counts:
        if not number:
            continue
        each = [dict(ref.each).get(kind) for ref in refs]  # type: ignore[union-attr]
        if any(price is None for price in each):
            raise RepriceError(422, "This fare can't be priced for these passengers. Search again.", code="adults_only")
        priced.append((kind, number, sum(each)))  # type: ignore[arg-type]
    return wanted, currencies.pop(), priced, {"offer_ids": [ref.offer_id for ref in refs], "transaction_id": refs[0].transaction_id}  # type: ignore[union-attr]


async def price_selection(request: FlightSearchRequest, wanted: list[FareRef], client: SouthairClient) -> tuple[str, list[FareRef], str, list[tuple[str, int, int]]]:
    """Repeat the search, find the same flights and fares, and price them.

    Returns (nld, fresh refs, currency, [(pax type, count, supplier cost each in minor units)]).
    Mixed passengers need per-type totals from fare_info: explicit TOTALs, or the per-type sums of
    the components when they add up exactly to FINAL_TOTAL. Adults alone use the search's adult
    total verified against FINAL_TOTAL. Anything else is refused rather than guessed.
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
            raise RepriceError(409, "This fare is no longer available. Search again and choose another fare.", code="fare_gone")
        fresh.append(FareRef(ref.direction, ref.route_key, ref.flight_key, fare.code, fare.fare_hash, fare.adult_total,
                             segment(ref.direction, leg, flight, fare)))

    fallback = await account_currency(client)
    counts = [("ADT", request.adults), ("CHD", request.children), ("INF", request.infants)]
    fare_info: dict | None = None
    try:
        fare_info = await client.fare_info(nld=nld, selections=[(r.route_key, r.fare_hash) for r in fresh])
    except SouthairError as error:
        logger.info("Fare info before booking unavailable", extra={"error_code": error.error_code})
    per_type = normalize.fare_info_totals(fare_info) if isinstance(fare_info, dict) else None
    final = normalize.fare_info_final_total(fare_info)
    if not (per_type and all(kind in per_type for kind, number in counts if number)) and (request.children or request.infants):
        # Live UAT: no per-type TOTAL. Per-type sums of the components, used only when they add up
        # exactly to the supplier's FINAL_TOTAL for this passenger mix; otherwise refused below.
        components = normalize.fare_info_component_totals(fare_info)
        if (components and final is not None and all(kind in components for kind, number in counts if number)
                and sum(to_minor(components[kind].value) * number for kind, number in counts if number) == to_minor(final.value)):
            per_type = {kind: normalize.Amount(components[kind].value, final.currency) for kind in components}
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
        raise RepriceError(422, "Child and infant fares can't be confirmed for this fare yet. Search for adults only.", code="adults_only")
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


def search_selling_total(request: FlightSearchRequest, wanted: list[FareRef]) -> int | None:
    """Selling total at the search prices: adults only, from the search's per-adult totals.

    Children and infants had no search price, so the old total is unknown for them (None).
    """
    if request.children or request.infants or not all(ref.adult_total for ref in wanted):
        return None
    each = sell(sum(to_minor(ref.adult_total.value) for ref in wanted))  # type: ignore[union-attr]
    if each is not None and wanted[0].provider == SOOPERFARE:
        each += sf_search_share(request.adults)
    return each * request.adults if each is not None else None


def price_change_allowed(old_total: int | None, new_total: int) -> bool:
    """The config point for a cap on price changes (FLIGHT_PRICE_CHANGE_LIMIT_MINOR). No cap: always allowed.

    With a cap, an increase above it is refused; an unknown old total can't be checked and is refused.
    """
    limit = settings.flight_price_change_limit_minor
    if limit is None:
        return True
    if old_total is None:
        return False
    return new_total - old_total <= limit


def booking_enabled(provider: str) -> bool:
    if provider == KYTE:
        return False  # Kyte fares are requests: staff confirm and book (Kyte booking and payment come next)
    return settings.sooperfare_booking_enabled if provider == SOOPERFARE else settings.southair_booking_enabled


def can_book(viewer: BackOfficeUser, provider: str = "southair") -> bool:
    from app.core.field_visibility import BACK_OFFICE_ROLES

    return booking_enabled(provider) and viewer.role in BACK_OFFICE_ROLES


_DOCUMENTS: dict[tuple[str, tuple[str, ...]], tuple[float, list[dict] | RepriceError]] = {}
DOCUMENTS_MAX = 2000  # cached selections; the oldest go first
DOCUMENTS_FAILURE_SECONDS = 60  # a failed read isn't retried at the supplier for this long


async def document_requirements(search_id: str, fare_ids: list[str], viewer: BackOfficeUser, sooperfare_factory=None) -> list[dict]:
    """Which travelers must give a CNIC or a passport for these fares, before the passenger form is sent (SooperFare's
    quote; other suppliers: none known up front). Kept for the quote's lifetime so a page reload doesn't ask again."""
    session = get_session(search_id, viewer.user_id)
    if session is None:
        raise RepriceError(404, "This search has expired. Search again.", code="search_expired")
    wanted = _selected_refs(session, fare_ids)
    provider = wanted[0].provider
    # The supplier is asked only for a fare that can really be held here (the same gate as checkout's payment options).
    if (provider != SOOPERFARE or not booking_enabled(provider)
            or any(restricted_airlines.segment_restricted(ref.segment) for ref in wanted)
            or (viewer.role == "website" and not settings.public_flight_booking_enabled)):
        return []
    key, now = (search_id, tuple(fare_ids)), _now()
    cached = _DOCUMENTS.get(key)
    if cached and cached[0] > now:
        if isinstance(cached[1], RepriceError):
            raise cached[1]
        return cached[1]
    from app.services.sooperfare_holds import required_documents

    try:
        _, _, _, context = await sf_price_selection(session.request, wanted, sooperfare_factory)
        result: list[dict] | RepriceError = required_documents(context.get("passengers") or [])
        until = now + QUOTE_TTL_SECONDS
    except (sooperfare.SooperfareError, RepriceError) as error:
        logger.warning("Document requirements not read", extra={"error_code": getattr(error, "error_code", None) or getattr(error, "code", None)})
        result = RepriceError(502, "The airline's document rules couldn't be read. Try again.")
        until = now + DOCUMENTS_FAILURE_SECONDS
    for old in [k for k, (expires, _) in _DOCUMENTS.items() if expires <= now]:
        _DOCUMENTS.pop(old, None)
    while len(_DOCUMENTS) >= DOCUMENTS_MAX:
        _DOCUMENTS.pop(min(_DOCUMENTS, key=lambda k: _DOCUMENTS[k][0]))
    _DOCUMENTS[key] = (until, result)
    if isinstance(result, RepriceError):
        raise result
    return result


async def reprice(search_id: str, fare_ids: list[str], viewer: BackOfficeUser, client_factory=None, sooperfare_factory=None,
                  kyte_factory=None) -> "RepriceView":
    from app.schemas.live_flights import PassengerPrice, RepriceView

    session = get_session(search_id, viewer.user_id)
    if session is None:
        raise RepriceError(404, "This search has expired. Search again.", code="search_expired")
    wanted = _selected_refs(session, fare_ids)
    restricted = any(restricted_airlines.segment_restricted(ref.segment) for ref in wanted)
    provider, context, fee = wanted[0].provider, None, 0
    try:
        if provider == SOOPERFARE:
            nld = ""
            fresh, currency, priced, context = await sf_price_selection(session.request, wanted, sooperfare_factory)
            fee = int(context.get("platform_fee_minor") or 0)
        elif provider == KYTE:
            nld = ""
            fresh, currency, priced, context = await ky_price_selection(session.request, wanted, kyte_factory)
        else:
            client = (client_factory or configured_client)()
            nld, fresh, currency, priced = await price_selection(session.request, wanted, client)
    except (SouthairError, sooperfare.SooperfareError, kyte.KyteError) as error:
        logger.warning("Reprice failed", extra={"error_code": error.error_code, "provider": provider})
        raise RepriceError(502, "The flight supplier couldn't reprice this fare. Try again.") from None
    passengers = []
    for kind, number, cost_each in priced:
        selling_each = sell(cost_each)
        if selling_each is None:
            raise RepriceError(409, "No selling price is configured for flights. Nothing was booked.", code="no_selling_price")
        if kind == "ADT":  # SooperFare's platform fee, passed on across the adults (0 for Southair)
            selling_each += fee_share(fee, number)
        passengers.append((kind, number, cost_each, selling_each))
    supplier_total = sum(number * cost for _, number, cost, _ in passengers) + fee  # SooperFare: + its platform fee per booking
    selling_total = sum(number * selling for _, number, _, selling in passengers)
    if selling_total < supplier_total:
        raise RepriceError(409, "The selling price doesn't cover the supplier's total for this fare. Nothing was booked.", code="no_selling_price")
    old_adult = sum(to_minor(r.adult_total.value) for r in wanted if r.adult_total)
    new_adult = sum(to_minor(r.adult_total.value) for r in fresh if r.adult_total)
    old_total = search_selling_total(session.request, wanted)
    price_changed = old_adult != new_adult or (old_total is not None and old_total != selling_total)
    if price_changed and not price_change_allowed(old_total, selling_total):
        raise RepriceError(409, f"The price changed from {money_text(old_total or 0, currency)} to {money_text(selling_total, currency)}, "
                                "more than the allowed change. Search again.", code="change_too_large")
    quote = Quote(
        id=secrets.token_urlsafe(16), user_id=viewer.user_id, request=session.request, nld=nld, refs=fresh, currency=currency,
        supplier_total_minor=supplier_total, selling_total_minor=selling_total, passengers=passengers,
        expires_at=_now() + QUOTE_TTL_SECONDS, price_changed=price_changed, old_total_minor=old_total,
        provider=provider, supplier_context=context,
    )
    _store_quote(quote)
    from app.services.sooperfare_holds import required_documents

    view = RepriceView(
        documents=required_documents((context or {}).get("passengers") or []),
        quote_id=quote.id, expires_at=datetime.fromtimestamp(quote.expires_at, timezone.utc),
        selling_total=FlightMoney(amount_minor=selling_total, currency=currency),
        new_total=FlightMoney(amount_minor=selling_total, currency=currency),
        old_total=FlightMoney(amount_minor=old_total, currency=currency) if old_total is not None else None,
        passengers=[PassengerPrice(type=kind, count=number, selling_each=FlightMoney(amount_minor=selling, currency=currency)) for kind, number, _, selling in passengers],
        price_changed=price_changed, booking_enabled=can_book(viewer, provider) and not restricted,
        booking_mode="request" if restricted or provider == KYTE else "hold",
    )
    old_each = sell(sum(to_minor(r.adult_total.value) for r in wanted)) if all(r.adult_total for r in wanted) else None  # type: ignore[union-attr]
    if old_each is not None and provider == SOOPERFARE:
        old_each += sf_search_share(session.request.adults)
    new_each = next((selling for kind, _, _, selling in passengers if kind == "ADT"), None)
    if old_each is not None:
        view.old_price_per_adult = FlightMoney(amount_minor=old_each, currency=currency)
    if new_each is not None:
        view.new_price_per_adult = FlightMoney(amount_minor=new_each, currency=currency)
    if old_each is not None and new_each is not None:
        view.price_difference_per_adult = FlightMoney(amount_minor=new_each - old_each, currency=currency)
    if viewer.visibility.cost_and_margin:
        view.supplier_total = FlightMoney(amount_minor=supplier_total, currency=currency)
        view.margin = FlightMoney(amount_minor=selling_total - supplier_total, currency=currency)
    return view
