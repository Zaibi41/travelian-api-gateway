from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.group_ticketing import GroupInventory, GroupLeg, GroupTicket
from app.vendors.group_ticketing.legs import mark_return


class DhilluSahiTicket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pnr: str | None = None
    date: str | None = None
    updated_at: str | None = None
    sector: str | None = None
    airline: str | None = Field(default=None, alias="air_line")
    airline_logo: str | None = Field(default=None, alias="air_line_logo")
    meal: str | int | None = None
    price: str | int | float | None = None
    ticket_type: str | int | None = None
    baggage: str | int | None = None
    hand_carry: str | int | None = Field(default=None, alias="departure_handcarry")
    sold: str | int | None = None
    remaining: int | None = None
    subpart: list[dict[str, Any]] = Field(default_factory=list)


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _codes(sector_name: Any) -> tuple[str | None, str | None]:
    parts = [part.strip().upper() for part in str(sector_name or "").split("-") if part.strip()]
    return (parts[0], parts[-1]) if len(parts) >= 2 else (None, None)


def _legs(ticket: "DhilluSahiTicket", group_sector: str | None = None) -> list[GroupLeg]:
    """Each subpart is one flight, in order: departure_* fields and the supplier's misspelt arivval_* fields (the arrival
    of that same flight). Every live ticket read on 15 Nov 2026 is one-way (ticket_type 1, one subpart). A return comes
    as more subparts: from the first one leaving the ticket's destination, they are the return flights."""
    legs = []
    for part in ticket.subpart:
        origin, destination = _codes(part.get("departure_sector_name") or ticket.sector)
        legs.append(GroupLeg(
            direction="outbound",
            flight_number=_text(part.get("departure_flight_number")), origin=origin, destination=destination,
            departure_date=_text(part.get("departure_date")), departure_time=_text(part.get("departure_time")),
            arrival_date=_text(part.get("arivval_date")), arrival_time=_text(part.get("arivval_time")),
            baggage=_text(part.get("departure_baggage")) or _text(ticket.baggage),
            hand_carry=_text(part.get("departure_handcarry")) or _text(ticket.hand_carry), meal=ticket.meal,
        ))
    return mark_return(legs, _codes(ticket.sector or group_sector)[1])


def _first_subpart(ticket: "DhilluSahiTicket", key: str) -> str | None:
    """A ticket's identity from its first subpart: group_id (the supplier group, e.g. 3056) or id (e.g. 10817).
    The listing row and ticket carry no ID of their own."""
    return _text(ticket.subpart[0].get(key)) if ticket.subpart else None


class DhilluSahiGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sector: str
    airline: dict[str, Any] = Field(default_factory=dict)
    tickets: list[DhilluSahiTicket] = Field(default_factory=list)


def normalize_groups(rows: list[dict[str, Any]], vendor_name: str = "dhillu_sahi", scope: str | None = None) -> list[GroupInventory]:
    """Validate supplier rows and map them to the gateway's normalized group shape. `scope` (the sector group id the
    rows came from) keeps row-position group ids apart when several listing calls are merged."""
    groups: list[GroupInventory] = []
    for group_index, row in enumerate(rows):
        group = DhilluSahiGroup.model_validate(row)
        airline = group.airline.get("name")
        airline_logo = group.airline.get("logo")
        tickets = [
            GroupTicket(
                vendor=vendor_name,
                pnr=ticket.pnr,
                departure_date=ticket.date,
                sector=ticket.sector or group.sector,
                airline=ticket.airline or airline,
                airline_logo=ticket.airline_logo or airline_logo,
                price=ticket.price,
                remaining=ticket.remaining,
                updated_at=ticket.updated_at,
                sold=ticket.sold,
                baggage=ticket.baggage,
                hand_carry=ticket.hand_carry,
                meal=ticket.meal,
                ticket_type=ticket.ticket_type,
                legs=_legs(ticket, group.sector),
                subparts=ticket.subpart,
                supplier_group_id=_first_subpart(ticket, "group_id"),
                supplier_ticket_id=_first_subpart(ticket, "id"),
            )
            for ticket in group.tickets
        ]
        groups.append(
            GroupInventory(
                id=f"{vendor_name}:{scope + ':' if scope else ''}{row.get('id', group_index)}",
                vendor=vendor_name,
                sector=group.sector,
                airline=airline,
                airline_logo=airline_logo,
                tickets=tickets,
            )
        )
    return groups
