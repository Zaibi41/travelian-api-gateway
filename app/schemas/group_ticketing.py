from typing import Any

from pydantic import BaseModel, Field


class GroupLeg(BaseModel):
    """One flight of a group fare, as the supplier describes it (normalized at the vendor boundary)."""

    direction: str = Field(description="outbound or return")
    flight_number: str | None = None
    origin: str | None = None
    destination: str | None = None
    departure_date: str | None = None
    departure_time: str | None = Field(None, description="HH:MM")
    arrival_date: str | None = None
    arrival_time: str | None = Field(None, description="HH:MM")
    baggage: str | None = Field(None, description="Checked baggage in kg as the supplier sends it, e.g. '20'.")
    hand_carry: str | None = Field(None, description="Hand carry in kg as the supplier sends it, e.g. '7'.")
    meal: str | int | None = None


class GroupTicket(BaseModel):
    vendor: str
    pnr: str | None = None
    departure_date: str | None = None
    sector: str | None = None
    airline: str | None = None
    airline_logo: str | None = None
    price: str | int | float | None = Field(None, description="Supplier price per adult, as sent.")
    currency: str | None = None
    remaining: int | None = None
    updated_at: str | None = None
    sold: str | int | None = None
    baggage: str | int | None = Field(None, description="Checked baggage of the outbound leg.")
    hand_carry: str | int | None = Field(None, description="Hand carry of the outbound leg.")
    meal: str | int | None = None
    ticket_type: str | int | None = None
    legs: list[GroupLeg] = Field(default_factory=list)
    subparts: list[dict[str, Any]] = Field(default_factory=list)
    #: Supplier-side identifiers a hold needs (Skypass ticket id and open_seats_slot). Never returned to callers.
    source_ticket_id: str | None = None
    source_slot: str | None = None


class GroupInventory(BaseModel):
    id: str
    vendor: str
    sector: str
    airline: str | None = None
    airline_logo: str | None = None
    tickets: list[GroupTicket]
