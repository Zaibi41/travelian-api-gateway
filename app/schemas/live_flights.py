"""Live flight search, reprice and hold responses (spec v3.0 D23, D24, D26).

Every ID here is opaque and short-lived. NLD, fare hashes and ticket references never
leave the gateway. `supplier` and `supplier_cost` are only set for viewers with the
matching permission; routes serialize with response_model_exclude_none.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class FlightMoney(BaseModel):
    amount_minor: int = Field(description="Integer minor units (paisa for PKR).")
    currency: str


class FareView(BaseModel):
    id: str = Field(description="Opaque fare ID, valid until the search expires.")
    name: str
    meal: str | None = None
    baggage: str | None = None
    refund: str | None = None
    time_limit: str | None = Field(None, description="Supplier's hold time text, e.g. '1 hours for domestic'.")
    selling_price: FlightMoney | None = Field(None, description="Per adult. Omitted when no markup is configured or the supplier sent no total.")
    supplier: str | None = Field(None, description="Only with the supplier names permission.")
    supplier_cost: FlightMoney | None = Field(None, description="Per adult. Only with the cost and margin permission.")


class FlightView(BaseModel):
    id: str = Field(description="Opaque flight ID.")
    airline_code: str | None = None
    airline_name: str | None = None
    flight_number: str | None = None
    departure_local: str | None = Field(None, description="Local time, YYYY-MM-DDTHH:MM.")
    arrival_local: str | None = None
    duration_minutes: int | None = None
    stops: int | None = None
    stop_cities: list[str] = []
    fares: list[FareView]


class JourneyView(BaseModel):
    direction: Literal["outbound", "return"]
    origin: str
    destination: str
    flights: list[FlightView]


class ProviderStatus(BaseModel):
    name: str
    status: Literal["responded", "failed", "not_configured"]


class FlightSearchView(BaseModel):
    search_id: str = Field(description="Opaque. Fare IDs from this search expire with it.")
    expires_at: datetime
    status: Literal["complete", "unavailable"]
    notice: str | None = Field(None, description='D24: "Some results may be missing" only when a provider fails and other results are returned; never with status "unavailable". No provider names.')
    journeys: list[JourneyView]
    providers: list[ProviderStatus] | None = Field(None, description="Only with the supplier names permission.")


class RepriceRequest(BaseModel):
    search_id: str = Field(min_length=8, max_length=64)
    fare_ids: list[str] = Field(min_length=1, max_length=2, description="Outbound fare, then the return fare for a round trip.")


class PassengerPrice(BaseModel):
    type: Literal["ADT", "CHD", "INF"]
    count: int
    selling_each: FlightMoney


class RepriceView(BaseModel):
    quote_id: str = Field(description="Opaque. Valid for 10 minutes, for the user who repriced.")
    expires_at: datetime
    selling_total: FlightMoney = Field(description="The total the user must confirm before a hold.")
    passengers: list[PassengerPrice]
    price_changed: bool = Field(description="True when the adult price differs from the search result.")
    booking_enabled: bool = Field(description="Whether this gateway may place a hold for this user.")
    supplier_total: FlightMoney | None = Field(None, description="Only with the cost and margin permission.")


_NAME = r"^[A-Za-z][A-Za-z '\-]{0,24}$"


class HoldPassenger(BaseModel):
    type: Literal["ADT", "CHD", "INF"]
    title: Literal["MR", "MRS", "MISS", "MASTER"]
    first_name: str = Field(pattern=_NAME)
    last_name: str = Field(pattern=_NAME)
    date_of_birth: date
    id_type: Literal["PSPT", "NATI"]
    document_number: str | None = Field(None, pattern=r"^[A-Za-z0-9]{1,15}$")
    document_country: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    document_expiry: date | None = None
    nationality: str | None = Field(None, pattern=r"^[A-Z]{3}$")


class HoldContact(BaseModel):
    email: str = Field(max_length=50, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    phone_code: str = Field(pattern=r"^[1-9]\d{0,3}$")
    phone: str = Field(pattern=r"^\d{5,10}$")


class HoldRequest(BaseModel):
    quote_id: str = Field(min_length=8, max_length=64)
    confirmed_total: FlightMoney = Field(description="The total the user confirmed. The hold is refused if the supplier's total differs.")
    contact: HoldContact
    passengers: list[HoldPassenger] = Field(min_length=1, max_length=9)


class HoldView(BaseModel):
    booking_id: str = Field(description="Travelian reference, e.g. TRV-AB12CD.")
    pnr: str
    time_limit: datetime | None = Field(None, description="The hold expires then unless ticketed. There is no cancel call.")
    selling_total: FlightMoney
    label: Literal["UAT test data"] = "UAT test data"
