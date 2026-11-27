"""SooperFare search / quote answers -> flights and fares (server side only).

Shape (read live 18 November 2026, tests/sooperfare/fixtures): data.ref_id (this search) and data.providers[].legs[].
Each "leg" is one bookable option with its own ref_id: one flight for a one-way search, an outbound and a return flight
for a return search (flights[0] leaves from the searched origin). Each flight has fares[] with ref_id, signature
("Value|EV|1"), name, passenger_fares[] ({traveler_type, total_passenger, total_price}: the total for that many
travelers of that type, in whole currency units), baggage_policies[] (carry / checked, kg) and fare_policies[]
(exchange / refund fees). A flight is identified across searches by its signature ("KHILHEPAPA400@2026-12-10 08:00")
and a fare by its signature; ref_ids are per search and stay on the server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.baggage import Baggage, from_sooperfare

TYPES = {"adult": "ADT", "child": "CHD", "infant": "INF"}


@dataclass(frozen=True)
class Fare:
    ref_id: str
    signature: str
    name: str
    currency: str
    #: Total supplier price per passenger type for this flight, in minor units: {"ADT": (count, total_minor)}.
    per_type: dict[str, tuple[int, int]]
    baggage: Baggage = field(default_factory=Baggage)
    refund: str | None = None
    change: str | None = None

    @property
    def adult_each_minor(self) -> int | None:
        count, total = self.per_type.get("ADT", (0, 0))
        return total // count if count else None


@dataclass(frozen=True)
class Flight:
    ref_id: str
    signature: str
    origin: str
    destination: str
    airline_code: str | None
    airline_name: str | None
    logo: str | None
    flight_number: str | None
    departure_local: str | None
    arrival_local: str | None
    duration_minutes: int | None
    stops: int
    stop_cities: list[str]
    segments: list[dict[str, Any]]
    fares: list[Fare]


@dataclass(frozen=True)
class Option:
    """One bookable option (SooperFare's "leg"): its flights, outbound first."""

    ref_id: str
    flights: list[Flight]


@dataclass
class Result:
    provider: str  # SooperFare's provider identifier (PA, PF, PK, ONEAPI, ...)
    search_ref_id: str
    options: list[Option] = field(default_factory=list)


def _minor(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def _local(value: Any) -> str | None:
    text = str(value or "")
    return text[:16] if len(text) >= 16 else None  # "2026-12-10T08:00:00+05:00" -> "2026-12-10T08:00" (local time)


def _policy(policies: list[dict], kind: str, currency: str) -> str | None:
    rows = [p for p in policies if p.get("type") == kind and p.get("traveler_type", "adult") == "adult"]
    if not rows:
        return None
    label = "Rs" if currency == "PKR" else currency
    return "; ".join(f"{p.get('description')}: {label} {int(p.get('price') or 0):,}" for p in rows if p.get("description"))[:300] or None


def parse_fare(raw: dict) -> Fare | None:
    currency = str(((raw.get("currency") or {}).get("code")) or "PKR").upper()
    per_type: dict[str, tuple[int, int]] = {}
    for row in raw.get("passenger_fares") or []:
        kind = TYPES.get(str(row.get("traveler_type")))
        count, total = row.get("total_passenger"), _minor(row.get("total_price"))
        if kind and isinstance(count, int) and count > 0 and total is not None and total > 0:
            per_type[kind] = (count, total)
    if not raw.get("ref_id") or not raw.get("signature") or "ADT" not in per_type:
        return None  # no reference or no adult price: not offered
    policies = raw.get("fare_policies") or []
    return Fare(ref_id=str(raw["ref_id"]), signature=str(raw["signature"]), name=str(raw.get("name") or raw.get("class") or "Fare").strip(),
                currency=currency, per_type=per_type, baggage=from_sooperfare(raw.get("baggage_policies")),
                refund=_policy(policies, "refund", currency) or ("Refundable (airline fees apply)" if raw.get("is_refundable") else "Non-refundable"),
                change=_policy(policies, "exchange", currency))


def parse_flight(raw: dict) -> Flight | None:
    segments = raw.get("segments") or []
    carrier = raw.get("marketing_carrier") or {}
    fares = [fare for fare in (parse_fare(f) for f in raw.get("fares") or []) if fare]
    if not raw.get("ref_id") or not raw.get("signature") or not segments or not fares:
        return None
    numbers = [str(s.get("flight_number")) for s in segments if s.get("flight_number")]
    stops = [str((s.get("to") or {}).get("iata")) for s in segments[:-1] if (s.get("to") or {}).get("iata")]
    return Flight(
        ref_id=str(raw["ref_id"]), signature=str(raw["signature"]),
        origin=str((raw.get("from") or {}).get("iata") or ""), destination=str((raw.get("to") or {}).get("iata") or ""),
        airline_code=carrier.get("iata"), airline_name=carrier.get("name"), logo=carrier.get("logo"),
        flight_number=" / ".join(numbers) or None, departure_local=_local(raw.get("departure_at")), arrival_local=_local(raw.get("arrival_at")),
        duration_minutes=raw.get("travel_time") if isinstance(raw.get("travel_time"), int) else None,
        stops=max(len(segments) - 1, 0), stop_cities=stops,
        segments=[{"flight_number": s.get("flight_number"), "operating": (s.get("operating_carrier") or {}).get("iata"),
                   "operating_name": (s.get("operating_carrier") or {}).get("name"),
                   "airline_code": (s.get("marketing_carrier") or {}).get("iata"), "airline_name": (s.get("marketing_carrier") or {}).get("name"),
                   "origin": (s.get("from") or {}).get("iata"),
                   "destination": (s.get("to") or {}).get("iata"), "departure_local": _local(s.get("departure_at")),
                   "arrival_local": _local(s.get("arrival_at")),
                   "duration_minutes": s.get("flight_time") if isinstance(s.get("flight_time"), int) and not isinstance(s.get("flight_time"), bool) else None}
                  for s in segments],
        fares=fares,
    )


def parse_search(answer: dict, origin: str, two_way: bool) -> list[Result]:
    data = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(data, dict) or not data.get("ref_id"):
        return []
    results = []
    for provider in data.get("providers") or []:
        identifier = str(((provider.get("provider") or {}).get("identifier")) or "")
        result = Result(provider=identifier, search_ref_id=str(data["ref_id"]))
        for leg in provider.get("legs") or []:
            if leg.get("fares_on_request") or not leg.get("ref_id"):
                continue
            flights = [flight for flight in (parse_flight(f) for f in leg.get("flights") or []) if flight]
            if len(flights) != (2 if two_way else 1) or flights[0].origin != origin:
                continue
            result.options.append(Option(ref_id=str(leg["ref_id"]), flights=flights))
        if result.options:
            results.append(result)
    return results


@dataclass(frozen=True)
class QuotedPassenger:
    ref_id: str
    kind: str  # ADT / CHD / INF
    required_fields: tuple[str, ...]


@dataclass(frozen=True)
class Quote:
    #: Per passenger type, summed over every flight: {"ADT": (count, total_minor)}.
    per_type: dict[str, tuple[int, int]]
    currency: str
    passengers: list[QuotedPassenger]
    passport_required: bool
    fare_signatures: list[str]
    #: SooperFare's own fee per booking (data.settings.platform_fee), added to the supplier cost. Minor units.
    platform_fee_minor: int = 0


def parse_quote(answer: dict) -> Quote | None:
    data = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(data, dict):
        return None
    totals: dict[str, list[int]] = {}
    currencies, signatures, passport = set(), [], False
    for provider in data.get("providers") or []:
        passport = passport or bool(provider.get("is_passport_required"))
        for leg in provider.get("legs") or []:
            fare = parse_fare(leg.get("fare") or {})
            if fare is None:
                return None
            currencies.add(fare.currency)
            signatures.append(fare.signature)
            for kind, (count, total) in fare.per_type.items():
                row = totals.setdefault(kind, [count, 0])
                if row[0] != count:
                    return None  # different passenger counts per flight: not one priced booking
                row[1] += total
    passengers = [QuotedPassenger(str(p.get("ref_id")), TYPES.get(str(p.get("type")), "?"), tuple(p.get("required_fields") or []))
                  for p in data.get("passengers") or [] if p.get("ref_id")]
    if not totals or len(currencies) != 1 or any(p.kind == "?" for p in passengers):
        return None
    fee = _minor((data.get("settings") or {}).get("platform_fee")) or 0
    return Quote(per_type={k: (v[0], v[1]) for k, v in totals.items()}, currency=currencies.pop(), passengers=passengers,
                 passport_required=passport, fare_signatures=signatures, platform_fee_minor=fee)
