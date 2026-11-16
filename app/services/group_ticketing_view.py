"""What each caller receives from Group Ticketing (spec v3.0 D23).

Everyone allowed to search: departure, route, airline, seats, baggage, meal, a
gateway-served logo and the selling price (listing price + configured markup; no
markup set means markup 0, so the listing price).
Only with "see supplier names": supplier and supplier PNR.
Raw supplier segments require both permissions because they may contain costs.
Only with "see cost and margin": supplier cost. Agency roles never get either.
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.field_visibility import FieldVisibility
from app.schemas.group_ticketing import GroupInventory, GroupSource, GroupTicket
from app.services import group_terms, supplier_logos
from app.services.group_ticketing import group_ticket_id, stock_label

from uuid import NAMESPACE_URL, uuid5


class GroupMoney(BaseModel):
    amount_minor: int = Field(description="Integer minor units (paisa for PKR).")
    currency: str


class GroupLegView(BaseModel):
    direction: Literal["outbound", "return"]
    flight_number: str | None = None
    origin: str | None = None
    destination: str | None = None
    departure_date: str | None = None
    departure_time: str | None = None
    arrival_date: str | None = None
    arrival_time: str | None = None
    baggage: str | None = Field(None, description="Checked baggage in kg, e.g. '20'.")
    hand_carry: str | None = Field(None, description="Hand carry in kg, e.g. '7'.")
    meal: Literal["Yes", "No"] | None = None


class GroupFareTerms(BaseModel):
    """Group fares are non-refundable and non-changeable. The texts to show, word for word (app.services.group_terms)."""

    version: str
    notice: str = Field(description="Above the results: the rule in one line.")
    acknowledgement: str = Field(description="The tick box on the staff hold form; this exact text is stored with the booking.")
    refund: Literal["Non-refundable"] = "Non-refundable"


class GroupTicketView(BaseModel):
    id: str
    fare_terms: GroupFareTerms = Field(default_factory=lambda: GroupFareTerms(
        version=group_terms.VERSION, notice=group_terms.TERM, acknowledgement=group_terms.STAFF_TEXT))
    updated_at: str | None = None
    sold: str | int | None = None
    departure_date: str | None = None
    departure_time: str | None = Field(None, description="HH:MM of the first segment, when the supplier sends it.")
    sector: str | None = None
    airline: str | None = None
    logo_url: str | None = Field(None, description="Gateway path for the airline logo.")
    flight_number: str | None = Field(None, description="Outbound flight number, e.g. 9P700.")
    remaining: int | None = None
    baggage: str | int | None = Field(None, description="Checked baggage of the outbound leg in kg.")
    hand_carry: str | int | None = Field(None, description="Hand carry of the outbound leg in kg.")
    meal: Literal["Yes", "No"] | None = None
    ticket_type: str | int | None = None
    legs: list[GroupLegView] = Field(default_factory=list, description="Every flight, outbound then return.")
    selling_price: GroupMoney | None = Field(None, description="Per adult: listing price + markup (0 when none is set). Omitted only when the provider sent no usable price.")
    stock: Literal["supplier", "owned"] = Field("supplier", description="Supplier stock or Travelian-owned stock. Never names the supplier.")
    can_hold: bool = Field(False, description="Whether Hold seats can be used on this fare: its source holds seats and it has a price.")
    hold_unavailable_reason: str | None = Field(None, description="Why Hold seats is off for this fare, when it is.")
    supplier: str | None = Field(None, description="Only with the supplier names permission.")
    pnr: str | None = Field(None, description="Supplier PNR. Only with the supplier names permission.")
    group_source: GroupSource | None = Field(None, description="Provider, supplier PNR, group and ticket IDs. Only with the supplier names permission.")
    subparts: list[dict[str, Any]] | None = Field(None, description="Raw supplier segments. Requires both supplier names and cost and margin permissions.")
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
    """Listing price + percent + fixed, rounded up to a whole unit. An unset markup counts as 0.
    None only when there is no listing price."""
    if cost is None:
        return None
    percent = settings.group_ticketing_markup_percent or Decimal("0")
    fixed = settings.group_ticketing_markup_fixed_minor or 0
    raw = Decimal(cost.amount_minor) * (1 + percent / 100) + fixed
    whole = (raw / 100).quantize(Decimal("1"), ROUND_CEILING) * 100
    return GroupMoney(amount_minor=int(whole), currency=cost.currency)


def _kilos(value: str | int | None) -> Decimal | None:
    text = str(value if value is not None else "").casefold().replace("kg", "").strip()
    try:
        number = Decimal(text) if text else None
    except InvalidOperation:
        return None
    return number if number is not None and number.is_finite() and number >= 0 else None


def baggage_text(checked: str | int | None, hand_carry: str | int | None) -> str | None:
    """Group baggage for display, hand carry first: 7 and 20 -> '7+20 kg'; hand carry only -> '7 kg';
    checked only -> '20 kg'. None when neither is a number."""
    bag, hand = _kilos(checked), _kilos(hand_carry)
    bag = bag if bag else None
    hand = hand if hand else None
    if hand is not None and bag is not None:
        return f"{hand.normalize():f}+{bag.normalize():f} kg"
    if hand is not None or bag is not None:
        return f"{(hand or bag).normalize():f} kg"
    return None


def _departure_time(ticket: GroupTicket) -> str | None:
    for part in ticket.subparts[:1]:
        value = str(part.get("departure_time") or "")
        if _TIME.match(value):
            return value[:5]
    return None


def meal_text(value: str | int | None) -> Literal["Yes", "No"] | None:
    """Suppliers send 1/0, 'Yes'/'No' or 'yes'/'no'; anything else is unknown."""
    text = str(value).strip().casefold() if value is not None else ""
    if text in ("1", "yes", "true", "y"):
        return "Yes"
    if text in ("0", "no", "false", "n"):
        return "No"
    return None


def _legs(ticket: GroupTicket) -> list[GroupLegView]:
    return [GroupLegView(direction="return" if leg.direction == "return" else "outbound", flight_number=leg.flight_number,
                         origin=leg.origin, destination=leg.destination, departure_date=leg.departure_date, departure_time=leg.departure_time,
                         arrival_date=leg.arrival_date, arrival_time=leg.arrival_time, baggage=leg.baggage, hand_carry=leg.hand_carry,
                         meal=meal_text(leg.meal) or meal_text(ticket.meal)) for leg in ticket.legs]


def ticket_source(ticket: GroupTicket) -> GroupSource | None:
    return GroupSource.of(ticket.vendor, ticket.pnr, ticket.supplier_group_id, ticket.supplier_ticket_id)


def ticket_view(ticket: GroupTicket, visibility: FieldVisibility) -> GroupTicketView:
    cost = supplier_cost(ticket)
    legs = _legs(ticket)
    first = next((leg for leg in legs if leg.direction == "outbound"), legs[0] if legs else None)
    view = GroupTicketView(
        id=group_ticket_id(ticket), updated_at=ticket.updated_at, sold=ticket.sold,
        departure_date=ticket.departure_date, departure_time=_departure_time(ticket) or (first.departure_time if first else None), sector=ticket.sector,
        airline=ticket.airline, logo_url=supplier_logos.register(ticket.airline_logo, supplier_logos.airline_label(ticket.airline, first.flight_number if first else None)),
        flight_number=first.flight_number if first else None,
        remaining=ticket.remaining, baggage=ticket.baggage, hand_carry=ticket.hand_carry, meal=meal_text(ticket.meal) or (first.meal if first else None),
        ticket_type=ticket.ticket_type, legs=legs, selling_price=selling_price(cost),
        stock=stock_label(ticket.vendor),
    )
    view.hold_unavailable_reason = hold_unavailable_reason(ticket.vendor, view.selling_price, visibility)
    view.can_hold = view.hold_unavailable_reason is None
    if visibility.supplier_names:
        view.supplier, view.pnr = ticket.vendor, ticket.pnr
        view.group_source = ticket_source(ticket)
    if visibility.supplier_names and visibility.cost_and_margin:
        view.subparts = ticket.subparts
    if visibility.cost_and_margin:
        view.supplier_cost = cost
    return view


NO_HOLD_PROVIDER = "Holding isn't available for this provider"
# Dhillu & Sahi's documented API (login, groups, check availability, add booking, booking
# detail) has no hold, release or cancel call. Names the supplier, so only for viewers with
# the supplier names permission; everyone else gets NO_HOLD_PROVIDER.
NO_HOLD_API = {"dhillu_sahi": "Dhillu & Sahi has no hold API", "al_haider": "Holding is not wired for Al Haider yet"}
NO_PRICE = "The provider sent no price for this fare"


def hold_unavailable_reason(vendor: str, price: GroupMoney | None, visibility: FieldVisibility | None = None) -> str | None:
    """Why Hold seats is off for a fare (None: it can be used). Checked again when the hold is placed."""
    from app.services.group_holds import provider_for  # late import: group_holds imports this module

    if provider_for(vendor) is None:
        if visibility is not None and visibility.supplier_names and vendor in NO_HOLD_API:
            return NO_HOLD_API[vendor]
        return NO_HOLD_PROVIDER
    if price is None:
        return NO_PRICE
    return None


def inventory_view(groups: list[GroupInventory], visibility: FieldVisibility) -> GroupInventoryView:
    return GroupInventoryView(data=[
        GroupView(
            id=str(uuid5(NAMESPACE_URL, f"travelian:group-ticketing:{group.vendor}:{group.id}")),
            sector=group.sector, airline=group.airline, logo_url=supplier_logos.register(group.airline_logo, group.airline),
            tickets=[ticket_view(ticket, visibility) for ticket in group.tickets],
        )
        for group in groups
    ])
