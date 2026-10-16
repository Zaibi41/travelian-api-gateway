from typing import Any

from pydantic import BaseModel, Field


class GroupTicket(BaseModel):
    vendor: str
    pnr: str | None = None
    departure_date: str | None = None
    sector: str | None = None
    airline: str | None = None
    airline_logo: str | None = None
    price: str | int | float | None = None
    currency: str | None = None
    remaining: int | None = None
    baggage: str | int | None = None
    meal: str | int | None = None
    ticket_type: str | int | None = None
    subparts: list[dict[str, Any]] = Field(default_factory=list)


class GroupInventory(BaseModel):
    id: str
    vendor: str
    sector: str
    airline: str | None = None
    airline_logo: str | None = None
    tickets: list[GroupTicket]
