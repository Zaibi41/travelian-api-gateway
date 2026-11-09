"""What each caller receives from Group Ticketing (spec v3.0 D23).

Everyone allowed to search: departure, route, airline, seats, baggage, meal, a
gateway-served logo and the selling price (supplier price + configured markup).
Only with "see supplier names": supplier, supplier PNR and raw supplier segments.
Only with "see cost and margin": supplier cost. Agency roles never get either.
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.field_visibility import FieldVisibility
from app.schemas.group_ticketing import GroupInventory, GroupTicket
from app.services import supplier_logos
from app.services.group_ticketing import group_ticket_id

from uuid import NAMESPACE_URL, uuid5


class GroupMoney(BaseModel):
    amount_minor: int = Field(description="Integer minor units (paisa for PKR).")
    currency: str


class GroupTicketView(BaseModel):
    id: str
    updated_at: str | None = None
    sold: str | int | None = None
    departure_date: str | None = None
    departure_time: str | None = Field(None, description="HH:MM of the first segment, when the supplier sends it.")
    sector: str | None = None
    airline: str | None = None
    logo_url: str | None = Field(None, description="Gateway path for the airline logo.")
    remaining: int | None = None
    baggage: str | int | None = None
    meal: str | int | None = None
    ticket_type: str | int | None = None
    selling_price: GroupMoney | None = Field(None, description="Per adult. Omitted when no markup is configured or the price is unknown.")
    supplier: str | None = Field(None, description="Only with the supplier names permission.")
    pnr: str | None = Field(None, description="Supplier PNR. Only with the supplier names permission.")
    subparts: list[dict[str, Any]] | None = Field(None, description="Raw supplier segments. Only with the supplier names permission.")
    supplier_cost: GroupMoney | None = Field(None, description="Only with the cost and margin permission.")


class GroupView(BaseModel):
    id: str
    sector: str
    airline: str | None = None
    logo_url: str | None = None
    tickets: list[GroupTicketView]


class GroupInventoryView(BaseModel):
    data: list[GroupView]


_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d")


def supplier_cost(ticket: GroupTicket) -> GroupMoney | None:
    try:
        amount = Decimal(str(ticket.price).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    currency = (ticket.currency or settings.group_ticketing_default_currency).strip().upper()
    return GroupMoney(amount_minor=int((amount * 100).quantize(Decimal("1"), ROUND_HALF_UP)), currency=currency)


def selling_price(cost: GroupMoney | None) -> GroupMoney | None:
    """Cost + percent + fixed, rounded up to a whole unit. None unless a markup is set."""
    percent, fixed = settings.group_ticketing_markup_percent, settings.group_ticketing_markup_fixed_minor
    if cost is None or (percent <= 0 and fixed <= 0):
        return None
    raw = Decimal(cost.amount_minor) * (1 + percent / 100) + fixed
    whole = (raw / 100).quantize(Decimal("1"), ROUND_CEILING) * 100
    return GroupMoney(amount_minor=int(whole), currency=cost.currency)


def _departure_time(ticket: GroupTicket) -> str | None:
    for part in ticket.subparts[:1]:
        value = str(part.get("departure_time") or "")
        if _TIME.match(value):
            return value[:5]
    return None


def ticket_view(ticket: GroupTicket, visibility: FieldVisibility) -> GroupTicketView:
    cost = supplier_cost(ticket)
    view = GroupTicketView(
        id=group_ticket_id(ticket), updated_at=ticket.updated_at, sold=ticket.sold,
        departure_date=ticket.departure_date, departure_time=_departure_time(ticket), sector=ticket.sector,
        airline=ticket.airline, logo_url=supplier_logos.register(ticket.airline_logo), remaining=ticket.remaining,
        baggage=ticket.baggage, meal=ticket.meal, ticket_type=ticket.ticket_type, selling_price=selling_price(cost),
    )
    if visibility.supplier_names:
        view.supplier, view.pnr, view.subparts = ticket.vendor, ticket.pnr, ticket.subparts
    if visibility.cost_and_margin:
        view.supplier_cost = cost
    return view


def inventory_view(groups: list[GroupInventory], visibility: FieldVisibility) -> GroupInventoryView:
    return GroupInventoryView(data=[
        GroupView(
            id=str(uuid5(NAMESPACE_URL, f"travelian:group-ticketing:{group.vendor}:{group.id}")),
            sector=group.sector, airline=group.airline, logo_url=supplier_logos.register(group.airline_logo),
            tickets=[ticket_view(ticket, visibility) for ticket in group.tickets],
        )
        for group in groups
    ])
