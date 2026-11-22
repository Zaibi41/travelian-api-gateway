"""Live flight search, reprice and hold responses (spec v3.0 D23, D24, D26).

Every ID here is opaque and short-lived. NLD, fare hashes and ticket references never
leave the gateway. `supplier` and `supplier_cost` are only set for viewers with the
matching permission; routes serialize with response_model_exclude_none.
"""

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.customer_card import CustomerCard
from app.schemas.saved_people import SaveOutcomeView


class FlightMoney(BaseModel):
    amount_minor: int = Field(description="Integer minor units (paisa for PKR).")
    currency: str


class FareView(BaseModel):
    id: str = Field(description="Opaque fare ID, valid until the search expires.")
    name: str
    meal: str | None = None
    baggage: str | None = Field(None, description="Short, hand carry first: '7 kg' (hand carry only), '7+20 kg', '7+1×23 kg', "
                                                   "'7 kg + 1 pc', or 'Baggage: check with airline'.")
    baggage_detail: str | None = Field(None, description="In full words, e.g. '7 kg hand carry + 20 kg checked'.")
    refund: str | None = None
    time_limit: str | None = Field(None, description="Supplier's hold time text, e.g. '1 hours for domestic'.")
    selling_price: FlightMoney | None = Field(None, description="Per adult. Omitted when no markup is configured or the supplier sent no total.")
    total_price: FlightMoney | None = Field(None, description="Selling total of this fare for every traveler searched (adults, "
                                            "children, infants), each passenger type at the supplier's own price for it. Omitted "
                                            "when the supplier didn't price every type searched.")
    passenger_prices: list["PassengerPrice"] | None = Field(None, description="The total's breakdown per passenger type "
                                                             "(count and selling price each). Sent with total_price.")
    supplier: str | None = Field(None, description="Only with the supplier names permission.")
    supplier_cost: FlightMoney | None = Field(None, description="Per adult. Only with the cost and margin permission.")
    booking_mode: Literal["hold", "request"] = Field("hold", description="request: a restricted airline (D32). No supplier hold "
                                                                          "is ever made; it can only be sent as a request to book.")


class SegmentView(BaseModel):
    """One flight of a connection, in order. Airports are IATA codes; times are each airport's local time."""
    flight_number: str | None = None
    origin: str
    destination: str
    departure_local: str | None = Field(None, description="Local time at origin, YYYY-MM-DDTHH:MM.")
    arrival_local: str | None = Field(None, description="Local time at destination, YYYY-MM-DDTHH:MM.")
    duration_minutes: int | None = Field(None, description="Flying time of this flight.")
    layover_minutes: int | None = Field(None, description="Time on the ground at `origin` before this flight (the stop). "
                                                          "Omitted for the first flight.")


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
    segments: list[SegmentView] = Field([], description="Each flight of the connection, in order, with the time at each stop. "
                                                        "Empty when the supplier sent no flight-by-flight detail.")
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


class RequiredDocument(BaseModel):
    """A traveler whose airline asks for a specific document on this fare (SooperFare's quote: required_fields)."""
    type: Literal["ADT", "CHD", "INF"]
    number: int = Field(ge=1, description="1 for the first traveler of this type, 2 for the second, ... (booking order).")
    document: Literal["cnic", "passport"] = Field(description="cnic: a Pakistani CNIC / B-form (13 digits); passport: a passport.")


class RepriceView(BaseModel):
    quote_id: str = Field(description="Opaque. Valid for 10 minutes, for the user who repriced.")
    expires_at: datetime
    selling_total: FlightMoney = Field(description="The total the user must confirm before a hold.")
    passengers: list[PassengerPrice]
    price_changed: bool = Field(description="True when the revalidated total differs from the search result. A hold then needs accept_price_change.")
    old_total: FlightMoney | None = Field(None, description="Selling total at the search prices. Omitted when the search had no price for every passenger type.")
    new_total: FlightMoney = Field(description="The verified selling total now (same as selling_total).")
    booking_enabled: bool = Field(description="Whether this gateway may place a hold for this user.")
    booking_mode: Literal["hold", "request"] = Field("hold", description="request: restricted airline (D32): send a request to book, never a hold.")
    supplier_total: FlightMoney | None = Field(None, description="Only with the cost and margin permission.")
    old_price_per_adult: FlightMoney | None = Field(None, description="Selling price per adult at the search prices (all legs).")
    new_price_per_adult: FlightMoney | None = Field(None, description="Selling price per adult now (all legs). Omitted with no adult.")
    price_difference_per_adult: FlightMoney | None = Field(None, description="New less old price per adult; negative when the price fell.")
    margin: FlightMoney | None = Field(None, description="Selling total less supplier total. Only with the cost and margin permission.")
    documents: list[RequiredDocument] = Field(default_factory=list, description="Travelers who must give a CNIC or a passport for "
                                                                                 "this fare; ask for it before the hold.")


_NAME = r"^[A-Za-z][A-Za-z '\-]{0,24}$"


IDENTITY_FIELDS = ("date_of_birth", "id_type", "document_number", "document_country", "nationality", "document_expiry")


class HoldPassenger(BaseModel):
    """Our checks (the supplier and airline may add their own): passport needs number, issuing
    country and expiry; a Pakistani national ID (CNIC / B-form) is 13 digits. Expiry against the
    travel dates is checked when the hold is placed.

    With traveler_id (a saved person) the identity fields are left out: the gateway loads them on
    the server, checks the caller's scope and runs the same checks. Identity values sent together
    with traveler_id are refused (identity_with_traveler_id)."""

    type: Literal["ADT", "CHD", "INF"]
    title: Literal["MR", "MRS", "MISS", "MASTER"] | None = None
    first_name: str | None = Field(None, pattern=_NAME)
    last_name: str | None = Field(None, pattern=_NAME)
    date_of_birth: date | None = None
    id_type: Literal["PSPT", "NATI"] | None = None
    document_number: str | None = Field(None, pattern=r"^[A-Za-z0-9]{1,15}$")
    document_country: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    document_expiry: date | None = None
    nationality: str | None = Field(None, pattern=r"^[A-Z]{3}$")
    travels_with: int | None = Field(None, ge=0, le=8, description="Infants only: the adult it travels with, counted among adults from 0 (Adult 1 = 0). Default: infant n with adult n.")
    traveler_id: str | None = Field(None, min_length=8, max_length=36, description="A saved person; identity fields are then loaded by the gateway.")
    document_id: str | None = Field(None, min_length=8, max_length=36, description="With traveler_id: which saved document (needed when the person has several).")
    save_for_next_time: bool = Field(True, description="Create or update the saved person after the hold. Never blocks the hold.")

    @model_validator(mode="after")
    def document_rules(self):
        if self.travels_with is not None and self.type != "INF":
            raise ValueError(f"{self.first_name or 'Traveler'}: only an infant travels with an adult.")
        if self.traveler_id is not None:
            return self  # checked by the gateway after loading the saved person
        missing = [name for name in ("title", "first_name", "last_name", "date_of_birth", "id_type") if getattr(self, name) is None]
        if missing:
            raise ValueError(f"Required: {', '.join(missing)}.")
        if self.id_type == "PSPT":
            missing = [label for label, value in (("passport number", self.document_number), ("issuing country", self.document_country),
                                                  ("passport expiry", self.document_expiry)) if not value]
            if missing:
                raise ValueError(f"{self.first_name}: {', '.join(missing)} required for a passport.")
        if self.id_type == "NATI" and self.nationality == "PAK" and not re.fullmatch(r"\d{13}", self.document_number or ""):
            raise ValueError(f"{self.first_name}: a Pakistani national ID (CNIC or B-form) is 13 digits.")
        return self


class HoldContact(BaseModel):
    """The customer: who pays and is contacted (spec 8). Saved with the hold.

    With customer_id (a saved person) the fields may be left out and are loaded by the gateway;
    fields sent override them for this booking, and a changed phone or email also updates the
    saved person."""

    first_name: str | None = Field(None, pattern=_NAME)
    last_name: str | None = Field(None, pattern=_NAME)
    email: str | None = Field(None, max_length=50, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    phone_code: str | None = Field(None, pattern=r"^[1-9]\d{0,3}$")
    phone: str | None = Field(None, pattern=r"^\d{5,10}$")
    customer_id: str | None = Field(None, min_length=8, max_length=36, description="A saved person to use as the customer.")
    save_for_next_time: bool = Field(True, description="Create or update the saved customer after the hold. Never blocks the hold.")

    @model_validator(mode="after")
    def required_without_saved_customer(self):
        if self.customer_id is None:
            missing = [name for name in ("first_name", "last_name", "email", "phone_code", "phone") if not getattr(self, name)]
            if missing:
                raise ValueError(f"Required: {', '.join(missing)}.")
        return self


class HoldRequest(BaseModel):
    quote_id: str = Field(min_length=8, max_length=64)
    confirmed_total: FlightMoney = Field(description="The total the user confirmed. The hold is refused if the supplier's total differs.")
    contact: HoldContact | None = Field(None, description="The customer, already split (first/last name, phone code and number).")
    customer: CustomerCard | None = Field(None, description="Or the customer card shared with the Group Ticketing hold (full name, one phone "
                                                             "field); the gateway splits it. Send contact or customer, not both.")
    passengers: list[HoldPassenger] = Field(min_length=1, max_length=9)
    accept_price_change: bool = Field(False, description="Required (true) when the quote's price changed since the search.")

    @model_validator(mode="after")
    def one_customer(self):
        if (self.contact is None) == (self.customer is None):
            raise ValueError("Send the customer as contact or as customer, not both.")
        return self


class HoldView(BaseModel):
    booking_id: str = Field(description="Travelian reference, e.g. TRV-AB12CD.")
    pnr: str
    time_limit: datetime | None = Field(None, description="The hold expires then unless ticketed. There is no cancel call.")
    selling_total: FlightMoney
    supplier_cost: FlightMoney | None = Field(None, description="The airline's cost total. Only with the cost and margin permission.")
    profit: FlightMoney | None = Field(None, description="selling_total - supplier_cost. Only with the cost and margin permission.")
    label: str | None = Field(None, description="Not used: holds carry no label (issue #79).")
    warnings: list[str] = Field(default_factory=list, description="Not errors: e.g. passport valid for under 6 months on an international route.")
    saved_people: list[SaveOutcomeView] = Field(default_factory=list, description="What happened to the saved customer and travelers (never blocks the hold).")


FareView.model_rebuild()  # passenger_prices refers to PassengerPrice, defined further down
