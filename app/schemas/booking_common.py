"""Booking money and statuses, shared by the Bookings and the saved-people responses (no imports of other schemas)."""

from typing import Literal

from pydantic import BaseModel, Field

BookingStatus = Literal["held", "pending_payment", "payment_received", "confirmed", "ticket_issued", "cancelled", "expired", "hold_unrecorded", "requested"]
PaymentStatus = Literal["paid", "unpaid", "part_paid"]
IssuanceStatus = Literal["not_issued", "requested", "processing", "issued", "unknown"]


class BookingMoney(BaseModel):
    amount_minor: int = Field(description="Amount in minor units (paisa for PKR).")
    currency: str = Field(min_length=3, max_length=3)


class BookingStatuses(BaseModel):
    booking: BookingStatus
    payment: PaymentStatus
    issuance: IssuanceStatus
