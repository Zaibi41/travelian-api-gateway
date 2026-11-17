"""Flight Operations lists (back office)."""

from datetime import datetime

from pydantic import BaseModel, Field


class GroupDeparture(BaseModel):
    booking_id: str
    departure_at: datetime = Field(description="First flight's departure (local time at the origin, Pakistan time zone assumed).")
    route: str | None = Field(None, description='e.g. "LHE → JED → LHE".')
    airline_name: str | None = None
    flight_number: str | None = None
    customer_or_agency: str | None = None


class GroupDeparturesWithoutFinalPnr(BaseModel):
    window_hours: int
    total: int
    items: list[GroupDeparture] = Field(description="Soonest departure first.")
