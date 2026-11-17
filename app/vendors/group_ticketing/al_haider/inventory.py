"""Al Haider group inventory (read-only).

Contract: docs/postman/Al Haider Travel.postman_collection.json (Bearer auth at collection level). Al Haider gave a
permanent access token, so there is no login call: AL_HAIDER_ACCESS_TOKEN is sent as `Authorization: Bearer <token>`
and is never logged or returned. Only GET /api/available/groups is used (query type, airline_id, sector, dept_date,
all empty = everything). Booking calls in the collection (create, cancel, confirm) are not built.

Response read live on 14 Nov 2026: {"groups": [{id, airline_id, available_no_of_pax, dept_date, arv_date, sector
("ISB-MCT-JED": airport codes), type ("KSA ONEWAY"), price (int, rupees), baggage (null), meal ("Yes"/"No"), pnr,
effective_dept_date, details: [{id, group_id, sr, flight_no ("9P-710"), flight_date, origin/destination (city names),
baggage ("20+5"), dept_time, arv_time}], airline: {airline_name, short_name, logo_url}}]}.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.schemas.group_ticketing import GroupInventory, GroupLeg, GroupTicket
from app.vendors.group_ticketing.legs import mark_return
from app.vendors.group_ticketing.skypass.inventory import split_baggage

NAME = "al_haider"


class AlHaiderError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "upstream_error") -> None:
        super().__init__(message)
        self.error_code = error_code
        self.public_message = "Supplier request failed."


class AlHaiderLeg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    sr: int | None = None
    flight_no: str | None = None
    flight_date: str | None = None
    origin: str | None = None
    destination: str | None = None
    baggage: str | None = None
    dept_time: str | None = None
    arv_time: str | None = None


class AlHaiderAirline(BaseModel):
    model_config = ConfigDict(extra="ignore")

    airline_name: str | None = None
    short_name: str | None = None
    logo_url: str | None = None


class AlHaiderGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    available_no_of_pax: int | None = None
    dept_date: str | None = None
    arv_date: str | None = None
    sector: str | None = None
    type: str | None = None
    price: int | float | str | None = None
    baggage: str | None = None
    meal: str | int | None = None
    pnr: str | None = None
    details: list[AlHaiderLeg] = Field(default_factory=list)
    airline: AlHaiderAirline = Field(default_factory=AlHaiderAirline)


def is_configured() -> bool:
    token = settings.al_haider_access_token
    return bool(settings.al_haider_base_url and token and token.get_secret_value())


def _codes(sector: str | None) -> list[str]:
    """'ISB-MCT-JED' -> ['ISB', 'MCT', 'JED']."""
    return [part.strip().upper() for part in (sector or "").split("-") if part.strip()]


def _clock(value: str | None) -> str | None:
    return value[:5] if value and len(value) >= 5 and value[2] == ":" else value or None


def _flight_number(value: str | None) -> str | None:
    """'9P-710' -> '9P710' (as the other suppliers show it)."""
    return (value or "").replace("-", "").replace(" ", "").upper() or None


def outbound_codes(codes: list[str]) -> list[str]:
    """The outbound airports: all of them for a one-way sector; for a round trip written out ('LHE-DMM-LHE',
    'LHE-MCT-JED-MCT-LHE') up to the turning point."""
    if len(codes) >= 3 and len(codes) % 2 == 1 and codes == codes[::-1]:
        return codes[: len(codes) // 2 + 1]
    return codes


def _legs(group: AlHaiderGroup, codes: list[str]) -> list[GroupLeg]:
    """Each detail is one flight, in `sr` order. Its origin and destination are city names, so the airport codes come
    from the sector when it names one more airport than there are flights ('ISB-MCT-JED', two flights)."""
    details = sorted(group.details, key=lambda leg: leg.sr or 0)
    by_codes = len(codes) == len(details) + 1
    legs = []
    for index, leg in enumerate(details):
        checked, hand = split_baggage(leg.baggage)
        legs.append(GroupLeg(
            direction="outbound", flight_number=_flight_number(leg.flight_no),
            origin=codes[index] if by_codes else leg.origin, destination=codes[index + 1] if by_codes else leg.destination,
            departure_date=leg.flight_date, departure_time=_clock(leg.dept_time), arrival_time=_clock(leg.arv_time),
            baggage=checked, hand_carry=hand, meal=group.meal,
        ))
    outbound = outbound_codes(codes)
    return mark_return(legs, outbound[-1] if len(outbound) >= 2 else None)


def normalize(rows: list[dict[str, Any]]) -> list[GroupInventory]:
    groups = []
    for row in rows:
        group = AlHaiderGroup.model_validate(row)
        codes = _codes(group.sector)
        legs = _legs(group, codes)
        first = legs[0] if legs else None
        sector = " - ".join(outbound_codes(codes)) or None
        details = sorted(group.details, key=lambda leg: leg.sr or 0)
        ticket = GroupTicket(
            vendor=NAME, pnr=group.pnr, departure_date=group.dept_date, sector=sector,
            airline=group.airline.airline_name, airline_logo=group.airline.logo_url,
            price=group.price,  # per passenger (see docs/api/group-ticketing-inventory.md)
            remaining=group.available_no_of_pax, meal=group.meal, ticket_type=group.type,
            baggage=first.baggage if first else None, hand_carry=first.hand_carry if first else None,
            legs=legs, subparts=[{**leg.model_dump(exclude_none=True), "departure_time": _clock(leg.dept_time)} for leg in details],
            supplier_group_id=str(group.id), supplier_ticket_id=str(details[0].id) if details and details[0].id is not None else None,
        )
        groups.append(GroupInventory(id=f"{NAME}:{group.id}", vendor=NAME, sector=sector or "", airline=group.airline.airline_name,
                                     airline_logo=group.airline.logo_url, tickets=[ticket]))
    return groups


async def fetch_inventory(transport: httpx.AsyncBaseTransport | None = None) -> list[GroupInventory]:
    if not is_configured():
        raise AlHaiderError("Al Haider is not configured", error_code="not_configured")
    token = settings.al_haider_access_token.get_secret_value()
    timeout = httpx.Timeout(settings.vendor_timeout_seconds, connect=min(settings.vendor_timeout_seconds, 3.0))
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.get(f"{settings.al_haider_base_url.rstrip('/')}/api/available/groups",
                                        params={"type": "", "airline_id": "", "sector": "", "dept_date": ""},
                                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise AlHaiderError("Al Haider groups request timed out", error_code="upstream_timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise AlHaiderError(f"Al Haider groups returned HTTP {exc.response.status_code}",
                            error_code=f"upstream_http_{exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise AlHaiderError("Al Haider groups request failed") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        raise AlHaiderError("Al Haider groups response did not include a groups array", error_code="invalid_response")
    try:
        return normalize(payload["groups"])
    except ValidationError as exc:
        raise AlHaiderError("Al Haider groups did not match the expected shape", error_code="invalid_response") from exc
