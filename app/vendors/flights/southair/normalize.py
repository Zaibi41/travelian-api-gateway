"""Southair search response -> normalized legs, flights and fares (server-side only).

Handles both shapes seen for fares:
- guide 2.2: flights.N.CLASS.<code> = {ACCEPTANCE, TARRIF: {ADT: {BASE, TAX, TOTAL}}, FEATURES}
- UAT (per the Postman collection): flights.N.AVB.CABIN.<code> = {STATUS, HASH, ...}
A fare without a usable hash is not offered. A fare without an explicit adult TOTAL has
no price (never computed from parts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_AMOUNT = re.compile(r"^\s*(-?\d[\d,]*(?:\.\d+)?)\s*([A-Z]{3})?\s*$")
_DURATION = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", re.I)


@dataclass(frozen=True)
class Amount:
    value: Decimal
    currency: str | None


def parse_amount(raw: Any) -> Amount | None:
    """"457.70", "50.00 SAR", 66000 -> Amount; anything else -> None."""
    if isinstance(raw, dict) and "VALUE" in raw:
        raw = raw["VALUE"]
    if isinstance(raw, bool) or raw is None:
        return None
    match = _AMOUNT.match(str(raw))
    if not match:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return Amount(value, match.group(2)) if value.is_finite() and value >= 0 else None


@dataclass(frozen=True)
class Fare:
    code: str
    fare_hash: str  # ACCEPTANCE / HASH: server-side only
    adult_total: Amount | None
    meal: str | None = None
    refund: str | None = None
    time_limit: str | None = None
    baggage: str | None = None


@dataclass(frozen=True)
class Flight:
    key: str  # stable across searches: callsign + local departure time
    airline_code: str | None
    airline_name: str | None
    flight_number: str | None
    departure_local: str | None
    arrival_local: str | None
    departure_utc: str | None
    duration_minutes: int | None
    stops: int | None
    stop_cities: list[str]
    fares: list[Fare]


@dataclass(frozen=True)
class Leg:
    origin: str
    destination: str
    route_key: str  # e.g. "KHIMUX", used by fare_info
    flights: list[Flight] = field(default_factory=list)


def _text(value: Any, limit: int = 120) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text[:limit] if text and text != "-" else None


def _local(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, pattern).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    return None


def _minutes(value: Any) -> int | None:
    text = _text(value)
    match = _DURATION.fullmatch(text.strip()) if text else None
    if not match or not any(match.groups()):
        return None
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def _features(fare: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in fare.get("FEATURES") or []:
        if isinstance(item, dict):
            title, text = _text(item.get("title"), 40), _text(item.get("service_text"))
            if title and text:
                found[title.lower()] = text
    return found


def _fare(code: str, raw: Any, uat_shape: bool) -> Fare | None:
    if not isinstance(raw, dict):
        return None
    if uat_shape:
        try:
            if int(raw.get("STATUS")) != 1:
                return None
        except (TypeError, ValueError):
            return None
        fare_hash = raw.get("HASH")
    else:
        fare_hash = raw.get("ACCEPTANCE")
    if not isinstance(fare_hash, str) or not fare_hash.strip() or fare_hash.strip().upper() == "NA":
        return None
    adult = (raw.get("TARRIF") or {}).get("ADT") if isinstance(raw.get("TARRIF"), dict) else None
    total = parse_amount(adult.get("TOTAL")) if isinstance(adult, dict) else None
    if total is not None and total.currency is None and isinstance(adult, dict):
        base = adult.get("BASE")
        base_fare = parse_amount(base.get("FARE")) if isinstance(base, dict) else None
        if base_fare and base_fare.currency:
            total = Amount(total.value, base_fare.currency)
    features = _features(raw)
    return Fare(
        code=str(code)[:16], fare_hash=fare_hash.strip(), adult_total=total,
        meal=features.get("meal"), refund=features.get("refund"), time_limit=features.get("time limit"),
        baggage=features.get("baggage"),
    )


def _flight(raw: Any) -> Flight | None:
    if not isinstance(raw, dict):
        return None
    main = raw.get("FLT_MAIN") if isinstance(raw.get("FLT_MAIN"), dict) else {}
    segments = [s for s in (raw.get("FLT_INFO") or {}).values() if isinstance(s, dict)] if isinstance(raw.get("FLT_INFO"), dict) else []
    operator = next((s.get("OPRT") for s in segments if isinstance(s.get("OPRT"), dict)), {}) or {}
    callsign = _text(main.get("callsign"), 16) or " / ".join(
        filter(None, (_text((s.get("INFO") or {}).get("callsign"), 16) for s in segments if isinstance(s.get("INFO"), dict)))
    ) or None
    cabins = (raw.get("AVB") or {}).get("CABIN") if isinstance(raw.get("AVB"), dict) else None
    uat_shape = isinstance(cabins, dict)
    source = cabins if uat_shape else raw.get("CLASS")
    fares = [fare for code, value in (source or {}).items() if (fare := _fare(code, value, uat_shape))] if isinstance(source, dict) else []
    if not fares:
        return None
    departure = _local(main.get("dep_date_time"))
    try:
        stops = int(main.get("STOP_COUNT")) if main.get("STOP_COUNT") not in (None, "") else None
    except (TypeError, ValueError):
        stops = None
    stop_text = _text(main.get("STOP_CITY"))
    code = _text(operator.get("iata_code"), 3) or (callsign[:2] if callsign and callsign[:2].isalnum() else None)
    return Flight(
        key=f"{callsign or '?'}@{departure or '?'}", airline_code=code.upper() if code else None,
        airline_name=_text(operator.get("name"), 80), flight_number=callsign, departure_local=departure,
        arrival_local=_local(main.get("arr_date_time")), departure_utc=_local(main.get("dep_dt_utc")),
        duration_minutes=_minutes(main.get("flight_time")), stops=stops,
        stop_cities=[c.strip() for c in stop_text.split(",") if c.strip()] if stop_text else [], fares=fares,
    )


def legs(data: dict[str, Any]) -> tuple[str | None, list[Leg]]:
    """(NLD, legs) from a search `data` object."""
    request = data.get("request") if isinstance(data.get("request"), dict) else {}
    nld = request.get("NLD") if isinstance(request.get("NLD"), str) and request.get("NLD") else None
    found: list[Leg] = []
    for sector in (data.get("result") or {}).values() if isinstance(data.get("result"), dict) else []:
        if not isinstance(sector, dict) or not isinstance(sector.get("itinerary"), dict):
            continue
        origin, destination = _text(sector["itinerary"].get("FROM"), 3), _text(sector["itinerary"].get("TO"), 3)
        if not origin or not destination:
            continue
        flights = [f for raw in (sector.get("flights") or {}).values() if (f := _flight(raw))] if isinstance(sector.get("flights"), dict) else []
        found.append(Leg(origin.upper(), destination.upper(), f"{origin.upper()}{destination.upper()}", flights))
    return nld, found


def fare_info_totals(data: dict[str, Any]) -> dict[str, Amount] | None:
    """Per passenger type totals from fare_info, only when each type has an explicit TOTAL."""
    totals: dict[str, Amount] = {}
    for kind in ("ADT", "CHD", "INF"):
        section = data.get(kind)
        if section is None:
            continue
        total = parse_amount(section.get("TOTAL")) if isinstance(section, dict) else None
        if total is None:
            return None
        totals[kind] = total
    return totals or None
