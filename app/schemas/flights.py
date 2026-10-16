from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
