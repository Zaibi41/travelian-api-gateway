"""Kyte Shop Flights / Offer Details answers -> flights and fares (server side only).

Shape (Kyte API 3.1.28, ShopFlightResponse): three collections keyed by id that reference each other.
- legs: JourneyLeg {id, departureAirport, arrivalAirport, flightSolutions: [FlightSolution ids]}
- flightSolutions: FlightSolution {id, journeyLegId, totalDuration (min), segments: [FlightSegment]}
  FlightSegment {id, marketingCarrier {code, name}, operatingCarrier?, flightNumber, duration (min),
  departure/arrival TimeAndPlace {airport {code, name}, date (YYYY-MM-DD...), time (HH:MM, airport local)}}
- offers: Offer {id, flightSolutions: [ids], singleJourneyLeg?, owner (airline code), expiration, totalPrice, currency
  {code, decimals}, passengers: [PricedPassenger {id, type adult|teen|child|infant, price}], fares: {id: Fare {name, ...}}}
All amounts are integers in the currency's smallest unit (`decimals` places). Several offers can share a flight
solution (one per fare brand): one flight is shown with each offer as a fare. Baggage isn't in the search answer
(Offer Details has it), so it is "not known" here, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services.baggage import Baggage

#: Kyte passenger types -> ours. A "teen" is priced as its own type by some airlines; we only sell ADT/CHD/INF.
TYPES = {"adult": "ADT", "teen": "ADT", "child": "CHD", "infant": "INF"}


def to_minor(amount: Any, decimals: Any) -> int | None:
    """Kyte's smallest-unit integer -> our minor units (two decimals). None when unreadable."""
    if isinstance(amount, bool) or not isinstance(amount, int) or isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        return None
    return int((Decimal(amount) * 100 / (Decimal(10) ** decimals)).quantize(Decimal("1"), ROUND_HALF_UP))


@dataclass(frozen=True)
class Fare:
    offer_id: str
    name: str
    currency: str
    #: Supplier price per passenger of each type, in minor units: {"ADT": 6123}. Missing type: not priced for it.
    each: dict[str, int]
    total_minor: int
    expiration: str | None = None
    baggage: Baggage = field(default_factory=Baggage)

    @property
    def adult_each_minor(self) -> int | None:
        return self.each.get("ADT")


@dataclass(frozen=True)
class Flight:
    signature: str  # the FlightSolution id
    direction: str
    origin: str
    destination: str
    airline_code: str | None
    airline_name: str | None
    flight_number: str | None
    departure_local: str | None
    arrival_local: str | None
    duration_minutes: int | None
    stops: int
    stop_cities: tuple[str, ...]
    segments: tuple[dict, ...]
    fares: tuple[Fare, ...]


def _local(place: dict | None) -> str | None:
    """TimeAndPlace -> "YYYY-MM-DDTHH:MM" (airport local time), or None."""
    if not isinstance(place, dict):
        return None
    day, clock = str(place.get("date") or "")[:10], str(place.get("time") or "")[:5]
    return f"{day}T{clock}" if len(day) == 10 and len(clock) == 5 else None


def _airport(place: dict | None) -> str | None:
    airport = place.get("airport") if isinstance(place, dict) else None
    code = airport.get("code") if isinstance(airport, dict) else None
    return str(code).upper() if code else None


def _segment(raw: dict) -> dict:
    carrier = raw.get("marketingCarrier") or {}
    operating = raw.get("operatingCarrier") or {}
    code = str(carrier.get("code") or "").upper() or None
    number = str(raw.get("flightNumber") or "").strip()
    flight_number = number if not code or number.upper().startswith(code) else f"{code}{number}"
    duration = raw.get("duration")
    return {
        "flight_number": flight_number or None, "airline_code": code, "airline_name": carrier.get("name"),
        "operating": str(operating.get("code") or "").upper() or None, "operating_name": operating.get("name"),
        "origin": _airport(raw.get("departure")), "destination": _airport(raw.get("arrival")),
        "departure_local": _local(raw.get("departure")), "arrival_local": _local(raw.get("arrival")),
        "duration_minutes": duration if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0 else None,
    }


def _fare(offer: dict) -> Fare | None:
    currency = offer.get("currency") or {}
    code, decimals = currency.get("code"), currency.get("decimals")
    total = to_minor(offer.get("totalPrice"), decimals)
    if not code or total is None:
        return None
    each: dict[str, int] = {}
    for passenger in offer.get("passengers") or []:
        kind = TYPES.get(str(passenger.get("type") or ""))
        price = to_minor(passenger.get("price"), decimals)
        if kind is None or price is None:
            return None  # a passenger we can't price: the offer is left out rather than guessed
        if kind in each and each[kind] != price:
            each[kind] = max(each[kind], price)  # the same type priced differently (e.g. teen vs adult): the higher price
        else:
            each.setdefault(kind, price)
    names = [str(f.get("name")).strip() for f in (offer.get("fares") or {}).values() if isinstance(f, dict) and f.get("name")]
    name = " / ".join(dict.fromkeys(names)) or "Fare"
    return Fare(offer_id=str(offer["id"]), name=name, currency=str(code).upper(), each=each, total_minor=total,
                expiration=offer.get("expiration"))


def _direction(leg: dict, origin: str, destination: str) -> str | None:
    """Outbound: leaves the searched origin or reaches the searched destination; return: the other way round."""
    dep, arr = str(leg.get("departureAirport") or "").upper(), str(leg.get("arrivalAirport") or "").upper()
    if dep == origin or arr == destination:
        return "outbound"
    if dep == destination or arr == origin:
        return "return"
    return None


def parse_shop(data: dict | None, origin: str, destination: str, *, two_way: bool) -> list[Flight]:
    """One Flight per (direction, flight solution) with every offer that covers exactly that solution as a fare.
    Return searches use split offers (one per direction, singleJourneyLeg); an offer covering both directions at once
    can't be shown as one outbound and one return fare, so it is left out."""
    if not isinstance(data, dict):
        return []
    legs = data.get("legs") or {}
    solutions = data.get("flightSolutions") or {}
    offers = data.get("offers") or {}
    if not isinstance(legs, dict) or not isinstance(solutions, dict) or not isinstance(offers, dict):
        return []
    fares_by_solution: dict[str, list[Fare]] = {}
    for offer in offers.values():
        if not isinstance(offer, dict) or not offer.get("id"):
            continue
        ids = [str(i) for i in offer.get("flightSolutions") or []]
        if len(ids) != 1 or (two_way and not offer.get("singleJourneyLeg")):
            continue
        fare = _fare(offer)
        if fare is not None:
            fares_by_solution.setdefault(ids[0], []).append(fare)
    flights: list[Flight] = []
    for solution_id, fares in fares_by_solution.items():
        solution = solutions.get(solution_id)
        if not isinstance(solution, dict):
            continue
        leg = legs.get(str(solution.get("journeyLegId") or ""))
        direction = _direction(leg, origin, destination) if isinstance(leg, dict) else None
        if direction is None or (direction == "return" and not two_way):
            continue
        segments = [_segment(raw) for raw in solution.get("segments") or [] if isinstance(raw, dict)]
        if not segments or not all(s["origin"] and s["destination"] for s in segments):
            continue
        first, last = segments[0], segments[-1]
        total = solution.get("totalDuration")
        if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            total = None
        fares.sort(key=lambda fare: fare.total_minor)
        flights.append(Flight(
            signature=solution_id, direction=direction, origin=first["origin"], destination=last["destination"],
            airline_code=first["airline_code"], airline_name=first["airline_name"], flight_number=first["flight_number"],
            departure_local=first["departure_local"], arrival_local=last["arrival_local"], duration_minutes=total,
            stops=len(segments) - 1, stop_cities=tuple(s["destination"] for s in segments[:-1]),
            segments=tuple(segments), fares=tuple(fares),
        ))
    return flights


def offer_details_total(data: dict | None, offer_id: str) -> tuple[int, str] | None:
    """The current total and currency Offer Details gives for one offer ((minor units, code)), or None."""
    details = (data or {}).get("details") if isinstance(data, dict) else None
    entry = details.get(offer_id) if isinstance(details, dict) else None
    if not isinstance(entry, dict):
        return None
    currency = entry.get("currency") or {}
    total = to_minor(entry.get("amount"), currency.get("decimals"))
    return (total, str(currency.get("code")).upper()) if total is not None and currency.get("code") else None


def search_errors(data: dict | None) -> list[str]:
    """Error codes in a 200 answer (SEARCH_ERROR = no availability; `source` names a failed airline)."""
    errors = (data or {}).get("errors") if isinstance(data, dict) else None
    return [str(e.get("code")) for e in errors or [] if isinstance(e, dict) and e.get("code")]
