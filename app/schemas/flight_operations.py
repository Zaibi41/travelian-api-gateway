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


class GroupAwaitingFinalPnrItem(BaseModel):
    booking_id: str
    departure_at: datetime | None = Field(None, description="First flight's departure (Pakistan time zone assumed); none when no flight is recorded.")
    departs_soon: bool = Field(description="Leaves within window_hours: the final PNR is due now.")
    route: str | None = Field(None, description='e.g. "LHE → JED → LHE".')
    airline_name: str | None = None
    flight_number: str | None = None
    customer_or_agency: str | None = None


class GroupAwaitingFinalPnr(BaseModel):
    """The Dashboard tracker (travelian#13): every paid group booking still waiting for the supplier's final PNR."""

    window_hours: int = Field(description="A booking leaving within this many hours is marked departs_soon.")
    total: int
    departs_soon: int = Field(description="How many of them leave within window_hours.")
    items: list[GroupAwaitingFinalPnrItem] = Field(description="Soonest departure first; bookings with no flight recorded last.")
