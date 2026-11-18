"""Group Ticketing fare calendar (B7, D33): every day of the range for one sector, each either with the lowest real
selling price the suppliers returned for that day, or marked as having no availability. A price is never invented,
copied from another day or interpolated: a day with no fare from a supplier has no price."""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, Field

from app.schemas.group_ticketing import GroupInventory
from app.services.group_ticketing import filter_group_inventory
from app.services.group_ticketing_view import GroupMoney, selling_price, supplier_cost

MAX_DAYS = 120


class CalendarDay(BaseModel):
    date: date
    available: bool
    lowest_price: GroupMoney | None = Field(None, description="Lowest selling price per adult that day; only when available.")
    fares: int = 0


class GroupCalendar(BaseModel):
    origin: str
    destination: str
    start: date
    end: date
    days: list[CalendarDay]


def build(groups: list[GroupInventory], origin: str, destination: str, adults: int, today: date, days: int = 60) -> GroupCalendar:
    matches = filter_group_inventory(groups, origin=origin, destination=destination, adults=adults)
    by_day: dict[date, list[GroupMoney]] = {}
    for group in matches:
        for ticket in group.tickets:
            try:
                day = date.fromisoformat(str(ticket.departure_date)[:10])
            except ValueError:
                continue
            price = selling_price(supplier_cost(ticket))
            if price is not None:
                by_day.setdefault(day, []).append(price)
    last_offered = max(by_day, default=today)
    end = min(max(today + timedelta(days=days - 1), last_offered), today + timedelta(days=MAX_DAYS - 1))
    calendar = []
    current = today
    while current <= end:
        prices = by_day.get(current, [])
        lowest = min(prices, key=lambda money: money.amount_minor) if prices else None
        calendar.append(CalendarDay(date=current, available=bool(prices), lowest_price=lowest, fares=len(prices)))
        current += timedelta(days=1)
    return GroupCalendar(origin=origin, destination=destination, start=today, end=end, days=calendar)
