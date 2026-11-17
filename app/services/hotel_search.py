"""Hotel search on RateHawk (spec v3.0 Search > Hotels; D23, D24, D25).

- Destination text or a chosen suggestion -> RateHawk region search, or hotel search for one hotel.
- RateHawk's search answer has rates but no hotel names: names, stars and city come from the Content
  API (100 hotels per call), cached in memory for CONTENT_TTL so repeat searches don't spend the limit.
- Selling price = supplier price + HOTEL_MARKUP_PERCENT (unset = 0), rounded up to a whole cent.
- Callers get opaque hotel and rate IDs bound to them for SEARCH_TTL; supplier hashes stay here.
  Provider name only with "see supplier names", supplier cost only with "see cost and margin".
- Hotel Inventory (manual hotels) isn't served by the gateway yet (docs/backend-todo.md); D25 merges
  it into this list when it is.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from fastapi import HTTPException

from app.core.config import settings
from app.core.field_visibility import BackOfficeUser
from app.schemas.hotels import (
    DestinationSuggestView, DestinationView, HotelMoney, HotelProviderStatus, HotelRateView, HotelSearchRequest,
    HotelSearchView, HotelView,
)
from app.vendors.hotels.ratehawk import normalize
from app.vendors.hotels.ratehawk.client import RateHawkClient, RateHawkError, RateHawkNotConfigured, configured_client

logger = logging.getLogger(__name__)

SEARCH_TTL_SECONDS = 15 * 60
MAX_SESSIONS = 500
#: Hotels kept per search (cheapest first). Each 100 needs one Content API call (limit 30 a minute).
MAX_HOTELS = 300
CONTENT_BATCH = 100
CONTENT_TTL_SECONDS = 24 * 3600
MAX_CONTENT_CACHE = 20_000
MISSING_NOTICE = "Some results may be missing."
PROVIDER_LABEL = "RateHawk"


@dataclass(frozen=True)
class RateRef:
    hid: int
    match_hash: str | None
    book_hash: str | None


@dataclass
class HotelSearchSession:
    id: str
    user_id: str
    request: HotelSearchRequest
    expires_at: float
    hotels: dict[str, int] = field(default_factory=dict)
    rates: dict[str, RateRef] = field(default_factory=dict)


_sessions: OrderedDict[str, HotelSearchSession] = OrderedDict()
_content: dict[int, tuple[float, normalize.HotelContent]] = {}


def _now() -> float:
    return time.time()


def _store(session: HotelSearchSession) -> None:
    now = _now()
    for key in [key for key, value in _sessions.items() if value.expires_at <= now]:
        del _sessions[key]
    _sessions[session.id] = session
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


def get_session(search_id: str, user_id: str) -> HotelSearchSession | None:
    session = _sessions.get(search_id)
    if session is None or session.user_id != user_id or session.expires_at <= _now():
        return None
    return session


# ---------- money ----------

def to_minor(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def selling_minor(cost: Decimal) -> int:
    percent = settings.hotel_markup_percent or Decimal("0")
    return int((cost * 100 * (1 + percent / 100)).quantize(Decimal("1"), ROUND_CEILING))


# ---------- suggestions ----------

async def suggest(query: str, client_factory=None) -> DestinationSuggestView:
    try:
        client = (client_factory or configured_client)()
        data = await client.suggest(query, settings.ratehawk_language)
    except RateHawkNotConfigured:
        raise HTTPException(503, "Hotel search isn't connected.") from None
    except RateHawkError as error:
        logger.warning("Hotel destination suggest failed", extra={"error_code": error.error_code})
        raise HTTPException(502, "Destinations can't be loaded right now.") from None
    return DestinationSuggestView(destinations=[DestinationView(**item) for item in normalize.destinations(data)[:20]])


# ---------- search ----------

def _guests(request: HotelSearchRequest) -> list[dict]:
    return [{"adults": room.adults, "children": list(room.children_ages)} for room in request.rooms]


async def _resolve(request: HotelSearchRequest, client: RateHawkClient) -> tuple[str, int, str]:
    """(kind, id, name). Free text takes RateHawk's first matching region, then its first hotel."""
    if request.destination_kind and request.destination_id:
        return request.destination_kind, request.destination_id, request.destination
    found = normalize.destinations(await client.suggest(request.destination, settings.ratehawk_language))
    for kind in ("region", "hotel"):
        match = next((item for item in found if item["kind"] == kind), None)
        if match:
            return kind, match["id"], match["name"]
    raise HTTPException(422, "Destination not found. Choose one from the list.")


async def _hotel_content(hids: list[int], client: RateHawkClient) -> tuple[dict[int, normalize.HotelContent], bool]:
    """Content for these hotels, from the cache or the Content API. Second value: a batch failed."""
    now = _now()
    found = {hid: entry[1] for hid in hids if (entry := _content.get(hid)) and now - entry[0] < CONTENT_TTL_SECONDS}
    missing = [hid for hid in hids if hid not in found]
    batches = [missing[i:i + CONTENT_BATCH] for i in range(0, len(missing), CONTENT_BATCH)]
    answers = await asyncio.gather(*(client.content_by_ids(batch, settings.ratehawk_language) for batch in batches),
                                   return_exceptions=True)
    failed = False
    for answer in answers:
        if isinstance(answer, Exception):
            failed = True
            logger.warning("Hotel content failed", extra={"error_code": getattr(answer, "error_code", type(answer).__name__)})
            continue
        for raw in answer:
            item = normalize.content(raw)
            if item:
                found[item.hid] = item
                _content[item.hid] = (now, item)
    if len(_content) > MAX_CONTENT_CACHE:
        for hid in sorted(_content, key=lambda key: _content[key][0])[: len(_content) - MAX_CONTENT_CACHE]:
            del _content[hid]
    return found, failed


def _rate_view(rate_id: str, rate: normalize.Rate, viewer: BackOfficeUser) -> HotelRateView:
    total = selling_minor(rate.amount)
    view = HotelRateView(
        id=rate_id, room_type=rate.room_name, board=rate.board, nights=rate.nights,
        total=HotelMoney(amount_minor=total, currency=rate.currency),
        per_night=HotelMoney(amount_minor=int((Decimal(total) / rate.nights).quantize(Decimal("1"), ROUND_CEILING)), currency=rate.currency),
        cancellation=rate.cancellation, free_cancellation_until=rate.free_cancellation_until,
    )
    if viewer.visibility.cost_and_margin:
        view.supplier_cost = HotelMoney(amount_minor=to_minor(rate.amount), currency=rate.currency)
    return view


async def search(request: HotelSearchRequest, viewer: BackOfficeUser, client_factory=None) -> HotelSearchView:
    session = HotelSearchSession(id=secrets.token_urlsafe(16), user_id=viewer.user_id, request=request,
                                 expires_at=_now() + SEARCH_TTL_SECONDS)
    expires_at = datetime.fromtimestamp(session.expires_at, timezone.utc)
    destination = request.destination
    try:
        client = (client_factory or configured_client)()
        kind, destination_id, destination = await _resolve(request, client)
        common = {
            "checkin": request.check_in.isoformat(), "checkout": request.check_out.isoformat(),
            "residency": settings.ratehawk_residency, "language": settings.ratehawk_language,
            "currency": settings.ratehawk_currency, "guests": _guests(request),
        }
        if kind == "hotel":
            data = await client.search_hotels(hids=[destination_id], **common)
        else:
            data = await client.search_region(region_id=destination_id, **common)
        found = normalize.search_results(data)
        found.sort(key=lambda hotel: min(rate.amount for rate in hotel.rates))
        found = found[:MAX_HOTELS]
        content, content_failed = await _hotel_content([hotel.hid for hotel in found], client)
    except RateHawkNotConfigured:
        return _unavailable(session, expires_at, destination, "not_configured", viewer)
    except RateHawkError as error:
        logger.warning("Hotel search failed", extra={"error_code": error.error_code})
        return _unavailable(session, expires_at, destination, "failed", viewer)

    hotels = []
    for hotel in found:
        info = content.get(hotel.hid)
        if info is None:
            continue  # no name to show
        hotel_id = secrets.token_urlsafe(9)
        session.hotels[hotel_id] = hotel.hid
        rate_views = []
        for rate in sorted(hotel.rates, key=lambda r: r.amount):
            rate_id = secrets.token_urlsafe(12)
            session.rates[rate_id] = RateRef(hotel.hid, rate.match_hash, rate.book_hash)
            rate_views.append(_rate_view(rate_id, rate, viewer))
        hotels.append(HotelView(
            id=hotel_id, name=info.name, star_rating=info.star_rating, city=info.city,
            country_code=info.country_code, address=info.address, rates=rate_views,
            source=PROVIDER_LABEL if viewer.visibility.supplier_names else None,
        ))
    _store(session)
    view = HotelSearchView(search_id=session.id, expires_at=expires_at, status="complete",
                           destination=destination, hotels=hotels)
    if content_failed and hotels:
        view.notice = MISSING_NOTICE
    if viewer.visibility.supplier_names:
        view.providers = [HotelProviderStatus(name=PROVIDER_LABEL, status="responded")]
    return view


def _unavailable(session: HotelSearchSession, expires_at: datetime, destination: str, status: str,
                 viewer: BackOfficeUser) -> HotelSearchView:
    view = HotelSearchView(search_id=session.id, expires_at=expires_at, status="unavailable",
                           destination=destination, hotels=[])
    if viewer.visibility.supplier_names:
        view.providers = [HotelProviderStatus(name=PROVIDER_LABEL, status=status)]
    return view
