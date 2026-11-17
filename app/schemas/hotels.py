"""Hotels search API (spec v3.0 Search > Hotels; D23 field visibility, D24 provider failure, D25)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

MAX_NIGHTS = 30


class DestinationSuggestRequest(BaseModel):
    query: str = Field(min_length=2, max_length=120, examples=["Dubai"])


class DestinationView(BaseModel):
    kind: Literal["region", "hotel"]
    id: int = Field(description="Pass back as destination_id with this kind.")
    name: str
    region_type: str | None = Field(None, description="For regions, e.g. City, Airport.")
    country_code: str | None = None


class DestinationSuggestView(BaseModel):
    destinations: list[DestinationView]


class RoomRequest(BaseModel):
    adults: int = Field(ge=1, le=6)
    children_ages: list[int] = Field(default_factory=list, max_length=4, description="One age (0-17) per child.")

    @model_validator(mode="after")
    def ages(self):
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("Each child's age must be between 0 and 17.")
        return self


class HotelSearchRequest(BaseModel):
    destination: str = Field(min_length=2, max_length=120, examples=["Dubai"],
                             description="Destination text. Without destination_id the gateway takes its first matching city.")
    destination_kind: Literal["region", "hotel"] | None = None
    destination_id: int | None = Field(None, gt=0)
    check_in: date
    check_out: date
    rooms: list[RoomRequest] = Field(min_length=1, max_length=9)

    @model_validator(mode="after")
    def validate(self):
        if self.check_out <= self.check_in:
            raise ValueError("Check-out must be after check-in.")
        if (self.check_out - self.check_in).days > MAX_NIGHTS:
            raise ValueError(f"A stay can be at most {MAX_NIGHTS} nights.")
        if self.check_in < date.today():
            raise ValueError("Check-in can't be in the past.")
        if (self.destination_id is None) != (self.destination_kind is None):
            raise ValueError("Send destination_kind and destination_id together.")
        return self


class HotelMoney(BaseModel):
    amount_minor: int = Field(description="Integer minor units (cents for USD, paisa for PKR).")
    currency: str


class HotelRateView(BaseModel):
    id: str = Field(description="Opaque rate ID, valid until the search expires.")
    room_type: str
    board: str
    nights: int
    total: HotelMoney = Field(description="Selling price for the whole stay, all rooms.")
    per_night: HotelMoney
    cancellation: Literal["free_until", "non_refundable", "charges_apply"]
    free_cancellation_until: datetime | None = Field(None, description="Supplier time (UTC), when cancellation is free until then.")
    supplier_cost: HotelMoney | None = Field(None, description="Whole stay. Only with the cost and margin permission.")


class HotelView(BaseModel):
    id: str = Field(description="Opaque hotel ID, valid until the search expires.")
    name: str
    star_rating: int | None = None
    city: str | None = None
    country_code: str | None = None
    address: str | None = None
    source: str | None = Field(None, description="D25: provider name or 'Hotel Inventory'. Only with the supplier names permission.")
    rates: list[HotelRateView] = Field(description="Cheapest first.")


class HotelProviderStatus(BaseModel):
    name: str
    status: Literal["responded", "failed", "not_configured"]


class HotelSearchView(BaseModel):
    search_id: str
    expires_at: datetime = Field(description="Rates and rate IDs from this search are valid until then.")
    status: Literal["complete", "unavailable"]
    notice: str | None = Field(None, description='D24: "Some results may be missing." when part of the results failed. No provider names.')
    destination: str = Field(description="The destination searched, as resolved.")
    hotels: list[HotelView]
    providers: list[HotelProviderStatus] | None = Field(None, description="Only with the supplier names permission.")
