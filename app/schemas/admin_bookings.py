"""Admin Bookings list and Booking detail responses.

Field visibility (spec v3.0 D23): `provider` (supplier names), `supplier_cost`,
`margin` and `supplier_payment` are LEFT OUT of the response (not null) for
callers without the matching permission. Routes serialize with
response_model_exclude_unset, so only fields that were set are sent.
Money is integer minor units with its currency code (bookings can be in PKR or
other currencies).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.admin_dashboard import FilterOption


Product = Literal["flight", "group", "hotel", "umrah"]
BookingStatus = Literal["held", "pending_payment", "payment_received", "confirmed", "ticket_issued", "cancelled", "expired"]
PaymentStatus = Literal["paid", "unpaid"]
IssuanceStatus = Literal["not_issued", "issued"]
Card = Literal["confirmed", "on_hold", "cancelled", "unpaid", "expiring_soon", "awaiting_issuance", "hold_deadlines"]
DateType = Literal["created"]  # travel date is not stored yet
DetailSection = Literal["itinerary", "passengers", "payments", "tickets", "servicing", "audit"]

SOURCE = "gateway_bookings"
SAMPLE_LABEL = "Sample data"


class BookingMoney(BaseModel):
    amount_minor: int = Field(description="Amount in minor units (paisa for PKR).")
    currency: str = Field(min_length=3, max_length=3)


class BookingStatuses(BaseModel):
    booking: BookingStatus
    payment: PaymentStatus
    issuance: IssuanceStatus


class BookingListCards(BaseModel):
    """Counts over the rows in the current filters (all but `card`). A card is omitted when its data doesn't exist yet."""

    all_bookings: int
    confirmed: int
    on_hold: int
    cancelled: int
    unpaid: int
    expiring_soon: int
    expiring_window_hours: int
    awaiting_issuance: int | None = Field(None, description="Flights tab only.")
    hold_deadlines: int | None = Field(None, description="Flights tab only: open holds not yet expired.")


class BookingListItem(BaseModel):
    booking_id: str
    pnr: str | None = None
    product: Product
    customer_or_agency: str | None = Field(None, description="Agency name when known. Customer names are not stored yet.")
    provider: str | None = Field(None, description="Supplier/source. Omitted without the supplier names permission.")
    statuses: BookingStatuses
    amount: BookingMoney
    created_at: datetime | None = None
    expires_at: datetime | None = None
    sample: bool = Field(description="True for rows from a one-time import (test data).")


class BookingListFilterOptions(BaseModel):
    agencies: list[FilterOption]
    providers: list[FilterOption] | None = Field(None, description="Omitted without the supplier names permission.")


class BookingList(BaseModel):
    source: Literal["gateway_bookings"] = SOURCE
    contains_sample_data: bool
    sample_label: Literal["Sample data"] = SAMPLE_LABEL
    cards: BookingListCards
    filter_options: BookingListFilterOptions
    total: int
    limit: int
    offset: int
    items: list[BookingListItem]


class BookingFare(BaseModel):
    billable_price: BookingMoney
    supplier_cost: BookingMoney | None = Field(None, description="Omitted without the cost and margin permission, or when unknown.")
    margin: BookingMoney | None = Field(None, description="Omitted without the cost and margin permission, or when unknown.")


class SupplierPayment(BaseModel):
    """The Supplier payment tab. Only sent with the cost and margin permission."""

    amount_due: BookingMoney | None = None
    hold_deadline: datetime | None = None


class BookingDetail(BaseModel):
    source: Literal["gateway_bookings"] = SOURCE
    sample: bool
    sample_label: Literal["Sample data"] | None = None
    booking_id: str
    pnr: str | None = None
    product: Product
    customer_or_agency: str | None = None
    provider: str | None = Field(None, description="Omitted without the supplier names permission.")
    statuses: BookingStatuses
    amount: BookingMoney
    deadline: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    fare: BookingFare
    supplier_payment: SupplierPayment | None = Field(None, description="Omitted without the cost and margin permission.")
    sections_not_available: list[DetailSection] = Field(
        description="Tabs whose data the gateway doesn't record yet; the page shows them as not available."
    )
