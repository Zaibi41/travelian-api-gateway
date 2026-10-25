from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.group_ticketing import GroupInventory, GroupTicket


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
    sold: str | int | None = None
    remaining: int | None = None
    subpart: list[dict[str, Any]] = Field(default_factory=list)


class DhilluSahiGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sector: str
    airline: dict[str, Any] = Field(default_factory=dict)
    tickets: list[DhilluSahiTicket] = Field(default_factory=list)


def normalize_groups(rows: list[dict[str, Any]], vendor_name: str = "dhillu_sahi") -> list[GroupInventory]:
    """Validate supplier rows and map them to the gateway's normalized group shape."""
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
                meal=ticket.meal,
                ticket_type=ticket.ticket_type,
                subparts=ticket.subpart,
            )
            for ticket in group.tickets
        ]
        groups.append(
            GroupInventory(
                id=f"{vendor_name}:{row.get('id', group_index)}",
                vendor=vendor_name,
                sector=group.sector,
                airline=airline,
                airline_logo=airline_logo,
                tickets=tickets,
            )
        )
    return groups
