"""Southair search response -> normalized legs, flights and fares (server-side only).

Handles both shapes seen for fares:
- guide 2.2: flights.N.CLASS.<code> = {ACCEPTANCE, TARRIF: {ADT: {BASE, TAX, TOTAL}}, FEATURES}
- UAT (per the Postman collection): flights.N.AVB.CABIN.<code> = {STATUS, HASH, ...}
- UAT as returned live (9 Nov 2026): no FLT_MAIN (times, STOP_COUNT and flight_time in
  minutes are on FLT_INFO.<route>.INFO), the per-adult total in ACC (equal to fare_info's
  FINAL_TOTAL for one adult, unchanged by the passenger mix) and features in
  HIGHLIGHT[].INFO {title, feature_code, info}.
A fare without a usable hash is not offered. A fare without an explicit adult total
(TARRIF.ADT.TOTAL or ACC) has no price (never computed from parts).
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
    """"01h 00m" or plain minutes (130 / "130") -> minutes."""
    text = _text(value)
    if text and text.isdigit():
        return int(text)
    match = _DURATION.fullmatch(text.strip()) if text else None
    if not match or not any(match.groups()):
        return None
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


#: HIGHLIGHT feature codes -> the feature names used below.
_FEATURE_CODES = {"checkin_baggage": "baggage"}
#: Supplier wording for "no checked bag" -> what we show. Anything else passes through.
_NO_CHECKED_BAGGAGE = re.compile(r"^\s*(nil+|no(ne)?|0\s*(kg|pc|pcs)?)(\s+(checked|checkin|check-in))?(\s+baggage)?\s*$", re.I)


def baggage_text(value: str | None) -> str | None:
    """Supplier checked-baggage text for display: "Nill Baggage" -> "No checked baggage"."""
    if value is None:
        return None
    return "No checked baggage" if _NO_CHECKED_BAGGAGE.match(value) else value


def _utc_minutes(start: Any, end: Any) -> int | None:
    begin, finish = _local(start), _local(end)
    if not begin or not finish:
        return None
    minutes = int((datetime.fromisoformat(finish) - datetime.fromisoformat(begin)).total_seconds() // 60)
    return minutes if minutes > 0 else None


def _features(fare: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in fare.get("FEATURES") or []:
        if isinstance(item, dict):
            title, text = _text(item.get("title"), 40), _text(item.get("service_text"))
            if title and text:
                found[title.lower()] = text
    for item in fare.get("HIGHLIGHT") or []:
        info = item.get("INFO") if isinstance(item, dict) else None
        if isinstance(info, dict):
            name = _FEATURE_CODES.get(str(info.get("feature_code") or "")) or (_text(info.get("title"), 40) or "").lower()
            text = _text(info.get("info"))
            if name and text:
                found.setdefault(name, text)
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
    if total is None and uat_shape:
        total = parse_amount(raw.get("ACC"))
    if total is not None and total.currency is None and isinstance(adult, dict):
        base = adult.get("BASE")
        base_fare = parse_amount(base.get("FARE")) if isinstance(base, dict) else None
        if base_fare and base_fare.currency:
            total = Amount(total.value, base_fare.currency)
    features = _features(raw)
    return Fare(
        code=str(code)[:16], fare_hash=fare_hash.strip(), adult_total=total,
        meal=features.get("meal"), refund=features.get("refund"), time_limit=features.get("time limit"),
        baggage=baggage_text(features.get("baggage")),
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
    infos = [s["INFO"] for s in segments if isinstance(s.get("INFO"), dict)]
    if not main and infos:
        # Live UAT: no FLT_MAIN. One segment carries everything; for several, take the
        # ends and count the connections (duration only from the UTC times).
        first, last = infos[0], infos[-1]
        main = {"dep_date_time": first.get("dep_date_time"), "arr_date_time": last.get("arr_date_time"),
                "dep_dt_utc": first.get("dep_dt_utc")}
        if len(infos) == 1:
            main.update({key: first.get(key) for key in ("STOP_COUNT", "STOP_CITY", "flight_time")})
        else:
            main["STOP_COUNT"] = len(infos) - 1
            main["STOP_CITY"] = ",".join(filter(None, (_text(info.get("arr_city"), 3) for info in infos[:-1])))
            main["flight_time"] = _utc_minutes(first.get("dep_dt_utc"), last.get("arr_dt_utc"))
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


def fare_info_final_total(data: dict[str, Any] | None) -> Amount | None:
    """fare_info's FINAL_TOTAL: the supplier's total for the whole selection and passenger mix.

    Live UAT sends this (and per-type components without a TOTAL); for one adult it equals
    the search's ACC. None when absent or unparseable: the total is then unverified.
    """
    return parse_amount(data.get("FINAL_TOTAL")) if isinstance(data, dict) else None


def fare_info_component_totals(data: dict[str, Any] | None) -> dict[str, Amount] | None:
    """Per passenger type totals summed from fare_info components (BASE.*.VALUE + TFC.*.VALUE).

    Live UAT sends components per type but only one FINAL_TOTAL. A component "-" is zero (e.g. an
    infant pays no taxes); any other unreadable value, or a non-empty TAX section (shape not seen),
    gives None. Callers must check the result against FINAL_TOTAL before using it.
    """
    if not isinstance(data, dict):
        return None
    totals: dict[str, Amount] = {}
    for kind in ("ADT", "CHD", "INF"):
        section = data.get(kind)
        if section is None:
            continue
        if not isinstance(section, dict) or section.get("TAX") not in (None, [], {}):
            return None
        total = Decimal(0)
        for group in ("BASE", "TFC"):
            parts = section.get(group) or {}
            if not isinstance(parts, dict):
                return None
            for part in parts.values():
                raw = part.get("VALUE") if isinstance(part, dict) else part
                if isinstance(raw, str) and raw.strip() == "-":
                    continue
                amount = parse_amount(raw)
                if amount is None:
                    return None
                total += amount.value
        totals[kind] = Amount(total, None)
    return totals or None
