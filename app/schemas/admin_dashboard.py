"""Admin Dashboard home responses. Money is integer paisa, per currency."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Period = Literal[
    "all", "today", "yesterday", "last-week", "last-two-weeks", "this-month",
    "last-month", "last-three-months", "last-six-months", "this-year", "last-year",
]
Product = Literal["flight", "group"]  # the filter: All (none), Flights or Group; hotel and umrah count under All
AttentionType = Literal["pending_issuance", "payment_pending", "hold_expiring"]


class NeedsAction(BaseModel):
    """Not affected by the date filter."""

    pending_issuance: int
    payment_pending: int
    expiring_holds: int
    expiring_window_hours: int


class CurrencyAmount(BaseModel):
    currency: str = Field(description="ISO currency code, e.g. PKR.")
    amount_minor: int | None = Field(None, description="Amount in minor units (paisa for PKR). Omitted = n/a: it can't be computed exactly.")
    profit_not_known: int | None = Field(None, description="Gross profit only: paid bookings left out because their supplier cost isn't known "
                                                         "(travelian#70; never counted as 0).")


class ProductResults(BaseModel):
    """One product's share of the business results (same rules, same filters)."""

    product: Product
    label: str
    total_bookings: int
    held: int
    paid: int
    issued_tickets: int
    total_sales: list[CurrencyAmount]
    gross_profit: list[CurrencyAmount] | None = Field(None, description="Omitted for roles without margin visibility.")


class BusinessResults(BaseModel):
    """Follows all filters. Money is summed per currency, never converted."""

    total_bookings: int
    held: int = Field(0, description="Holds still open (held or pending payment, expiry ahead).")
    paid: int = Field(0, description="Paid in full, not cancelled or expired.")
    issued_tickets: int
    total_sales: list[CurrencyAmount] = Field(description="One entry per currency of the paid bookings (PKR when there are none).")
    gross_profit: list[CurrencyAmount] | None = Field(
        None, description="Per currency, as total_sales. amount_minor omitted (n/a) when a paid booking in that currency has no real "
                          "supplier cost (none recorded, or an imported booking whose cost was filled in as its total). "
                          "Omitted for roles without margin visibility.",
    )
    by_product: list[ProductResults] | None = Field(
        None, description="Flights and Group separately, only when no product filter is set (All). Hotel and Umrah count in the totals only.",
    )


class DashboardSummary(BaseModel):
    source: Literal["gateway_bookings"] = "gateway_bookings"
    contains_sample_data: bool = Field(
        False, description="True while the bookings table holds rows from a one-time import (sample/test data)."
    )
    generated_at: datetime
    needs_action: NeedsAction
    business: BusinessResults


class ReportBucket(BaseModel):
    start: date
    bookings: int
    issued: int


class BookingReport(BaseModel):
    granularity: Literal["day", "week", "month"]
    timezone: str
    buckets: list[ReportBucket]


class AttentionItem(BaseModel):
    booking_id: str
    reference: str = Field(description="PNR when present, otherwise the booking ID.")
    type: AttentionType
    due_at: datetime | None = None


class AttentionQueue(BaseModel):
    total: int
    items: list[AttentionItem]


class FilterOption(BaseModel):
    value: str
    label: str


class DashboardFilterOptions(BaseModel):
    products: list[FilterOption]
    providers: list[FilterOption] | None = Field(None, description="Omitted unless the caller can see supplier names.")
