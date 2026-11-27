from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Multi-city: at most this many flights in one search (each searched as a one-way).
MAX_LEGS = 5


class SearchLeg(BaseModel):
    """One flight of a multi-city trip."""

    origin: str = Field(min_length=3, max_length=3, examples=["LHE"])
    destination: str = Field(min_length=3, max_length=3, examples=["DXB"])
    departure_date: date

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def uppercase_codes(cls, value: str) -> str:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def different_airports(self):
        if self.origin == self.destination:
            raise ValueError("origin and destination must be different")
        return self


class FlightSearchRequest(BaseModel):
    origin: str = Field(min_length=3, max_length=3, examples=["KHI"])
    destination: str = Field(min_length=3, max_length=3, examples=["DXB"])
    departure_date: date
    return_date: date | None = None
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=8)
    infants: int = Field(default=0, ge=0, le=8)
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    currency: str = Field(default="PKR", min_length=3, max_length=3)
    legs: list[SearchLeg] | None = Field(
        default=None, min_length=2, max_length=MAX_LEGS,
        description="Multi-city: every flight in order (2 to 5), each searched as a one-way. origin, destination and "
                    "departure_date must be the first flight's; return_date must be empty. Each journey of the answer "
                    "carries its `leg` number; choose one fare per leg.")

    @field_validator("origin", "destination", "currency", mode="before")
    @classmethod
    def uppercase_codes(cls, value: str) -> str:
        return value.upper() if isinstance(value, str) else value

    @field_validator("destination")
    @classmethod
    def destination_differs_from_origin(cls, value: str, info) -> str:
        origin = info.data.get("origin")
        if origin == value:
            raise ValueError("origin and destination must be different")
        return value

    @field_validator("return_date")
    @classmethod
    def return_after_departure(cls, value: date | None, info) -> date | None:
        departure = info.data.get("departure_date")
        if value is not None and departure is not None and value < departure:
            raise ValueError("return_date must be on or after departure_date")
        return value

    @model_validator(mode="after")
    def legs_in_order(self):
        if not self.legs:
            return self
        if self.return_date is not None:
            raise ValueError("a multi-city search has no return_date")
        first = self.legs[0]
        if (first.origin, first.destination, first.departure_date) != (self.origin, self.destination, self.departure_date):
            raise ValueError("origin, destination and departure_date must be the first leg's")
        for number, (previous, leg) in enumerate(zip(self.legs, self.legs[1:]), start=2):
            if leg.departure_date < previous.departure_date:
                raise ValueError(f"flight {number}'s date must be on or after flight {number - 1}'s")
        return self

    @property
    def multi_city(self) -> bool:
        return bool(self.legs)

    def leg_request(self, leg: SearchLeg) -> "FlightSearchRequest":
        """One multi-city flight as a one-way search with the same travelers and cabin."""
        return self.model_copy(update={"origin": leg.origin, "destination": leg.destination, "departure_date": leg.departure_date,
                                       "return_date": None, "legs": None})

    @property
    def last_travel_date(self) -> date:
        """The last day of travel: the return date, the last multi-city flight's date, or the departure date."""
        if self.legs:
            return self.legs[-1].departure_date
        return self.return_date or self.departure_date


class FlightSegment(BaseModel):
    airline: str
    flight_number: str
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    duration_minutes: int = Field(ge=0)
    cabin: str


class FlightOffer(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    vendor: str
    validating_airline: str
    price: Decimal = Field(ge=0)
    currency: str
    seats_available: int | None = Field(default=None, ge=0)
    segments: list[FlightSegment]


class VendorIssue(BaseModel):
    vendor: str
    code: str
    message: str


class VendorResult(BaseModel):
    vendor: str
    status: Literal["success", "failed", "timeout"]
    duration_ms: int = Field(ge=0)
    offer_count: int = Field(default=0, ge=0)
    error_code: str | None = None


class FlightSearchResponse(BaseModel):
    search_id: str
    status: Literal["complete", "partial", "unavailable"]
    offers: list[FlightOffer]
    vendors_queried: list[str]
    vendors_succeeded: list[str]
    vendors_failed: list[VendorIssue]
    vendor_results: list[VendorResult]
