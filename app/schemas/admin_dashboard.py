"""Admin Dashboard home responses. Money is integer paisa (PKR)."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Period = Literal[
    "all", "today", "yesterday", "last-week", "last-two-weeks", "this-month",
    "last-month", "last-three-months", "last-six-months", "this-year", "last-year",
]
Product = Literal["flight", "hotel", "umrah"]
AttentionType = Literal["pending_issuance", "payment_pending", "hold_expiring"]


class Money(BaseModel):
    amount_minor: int = Field(description="Amount in paisa.")
    currency: Literal["PKR"] = "PKR"


class NeedsAction(BaseModel):
    """Not affected by the date filter."""

    pending_issuance: int
    payment_pending: int
    expiring_holds: int
    expiring_window_hours: int


class BusinessResults(BaseModel):
    """Follows all filters. A field is omitted when it can't be computed exactly."""

    total_bookings: int
    issued_tickets: int
    total_sales: Money | None = Field(None, description="Omitted if any paid booking is not priced in PKR.")
    gross_profit: Money | None = Field(
        None, description="Omitted if any paid booking lacks supplier cost, or for roles without margin visibility."
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
    providers: list[FilterOption]
