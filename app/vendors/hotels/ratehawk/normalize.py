"""RateHawk search and content answers -> plain Travelian hotel records.

Supplier hashes (match_hash, book_hash) stay in these records for the gateway's own session; the
service decides what reaches the caller. Field names follow live sandbox answers (17 Nov 2026).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

#: RateHawk meal codes -> board text. Unknown codes are shown as words ("half-board" -> "Half board").
BOARD = {
    "nomeal": "Room only",
    "breakfast": "Breakfast",
    "breakfast-buffet": "Breakfast",
    "continental-breakfast": "Breakfast",
    "half-board": "Half board",
    "full-board": "Full board",
    "all-inclusive": "All inclusive",
}


@dataclass(frozen=True)
class Rate:
    match_hash: str | None
    book_hash: str | None
    room_name: str
    board: str
    amount: Decimal
    currency: str
    nights: int
    cancellation: str  # "free_until" | "non_refundable" | "charges_apply"
    free_cancellation_until: datetime | None


@dataclass(frozen=True)
class HotelRates:
    hid: int
    rates: list[Rate]


@dataclass(frozen=True)
class HotelContent:
    hid: int
    name: str
    star_rating: int | None
    city: str | None
    country_code: str | None
    address: str | None


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def board(meal: str | None) -> str:
    if not meal:
        return "Meal info not available"
    return BOARD.get(meal, meal.replace("-", " ").capitalize())


def _when(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _cancellation(penalties: dict | None, amount: Decimal) -> tuple[str, datetime | None]:
    penalties = penalties or {}
    free_until = _when(penalties.get("free_cancellation_before"))
    if free_until:
        return "free_until", free_until
    policies = penalties.get("policies") or []
    if any(not p.get("start_at") and (_decimal(p.get("amount_charge")) or 0) >= amount for p in policies):
        return "non_refundable", None
    return "charges_apply", None


def rate(raw: dict) -> Rate | None:
    payment_types = (raw.get("payment_options") or {}).get("payment_types") or []
    if not payment_types:
        return None
    payment = payment_types[0]
    amount = _decimal(payment.get("amount"))
    currency = payment.get("currency_code")
    if amount is None or not currency:
        return None
    kind, free_until = _cancellation(payment.get("cancellation_penalties"), amount)
    meal = raw.get("meal") or (raw.get("meal_data") or {}).get("value")
    return Rate(
        match_hash=raw.get("match_hash"), book_hash=raw.get("book_hash"),
        room_name=(raw.get("room_name") or "Room").strip(), board=board(meal),
        amount=amount, currency=str(currency).upper(), nights=max(len(raw.get("daily_prices") or []), 1),
        cancellation=kind, free_cancellation_until=free_until,
    )


def search_results(data: dict | None) -> list[HotelRates]:
    hotels = []
    for raw in (data or {}).get("hotels") or []:
        hid = raw.get("hid")
        if not isinstance(hid, int):
            continue
        rates = [r for r in (rate(item) for item in raw.get("rates") or []) if r]
        if rates:
            hotels.append(HotelRates(hid=hid, rates=rates))
    return hotels


def content(raw: dict) -> HotelContent | None:
    hid = raw.get("hid")
    name = (raw.get("name") or "").strip()
    if not isinstance(hid, int) or not name:
        return None
    region = raw.get("region") or {}
    stars = raw.get("star_rating")
    return HotelContent(
        hid=hid, name=name, star_rating=stars if isinstance(stars, int) and stars > 0 else None,
        city=region.get("name"), country_code=region.get("country_code"),
        address=raw.get("address"),
    )


def destinations(data: dict | None) -> list[dict]:
    """multicomplete -> [{kind, id, name, country_code}] regions first, then hotels."""
    out = []
    for region in (data or {}).get("regions") or []:
        if isinstance(region.get("id"), int) and region.get("name"):
            out.append({"kind": "region", "id": region["id"], "name": region["name"],
                        "region_type": region.get("type"), "country_code": region.get("country_code")})
    for hotel in (data or {}).get("hotels") or []:
        if isinstance(hotel.get("hid"), int) and hotel.get("name"):
            out.append({"kind": "hotel", "id": hotel["hid"], "name": hotel["name"], "region_type": None, "country_code": None})
    return out
