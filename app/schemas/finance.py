"""Finance API (travelian#91 and the Finance issues). Money is integer paisa (amount_minor) with its currency."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.booking_common import BookingMoney

RefundMethod = Literal["bank_transfer", "jazzcash", "1link", "payfast"]
RefundStatus = Literal["requested", "approved", "paid", "rejected"]


class FinanceAccess(BaseModel):
    finance: bool = Field(description="The caller has the Finance permission (Super Admin always; staff when granted).")


class AuditEntryView(BaseModel):
    id: int
    at: datetime
    actor_user_id: str | None = None
    actor_role: str | None = Field(None, description="The actor's role, or 'system' for automatic entries.")
    action: str
    entity_type: str
    entity_id: str | None = None
    booking_reference: str | None = None
    before: Any = None
    after: Any = None
    reason: str | None = None


class AuditLogPage(BaseModel):
    entries: list[AuditEntryView]
    total: int


class SupplierChargeView(BaseModel):
    id: str
    traveler_position: int | None = None
    traveler_name: str | None = None
    amount: BookingMoney
    source: Literal["issuance", "manual", "supplier_report"]
    supplier_reference: str | None = None
    reason: str | None = None
    recorded_by_user_id: str | None = None
    recorded_at: datetime


class AddSupplierChargeRequest(BaseModel):
    amount_minor: int = Field(description="Paisa. Negative corrects an earlier charge.")
    reason: str = Field(min_length=1, max_length=500)
    traveler_position: int | None = Field(None, ge=0)
    supplier_reference: str | None = Field(None, max_length=120)


class PaymentFeeView(BaseModel):
    id: str
    payment_id: str
    amount: BookingMoney
    source: Literal["recorded", "estimated", "settlement"]
    reason: str | None = None
    recorded_by_user_id: str | None = None
    recorded_at: datetime


class AddPaymentFeeRequest(BaseModel):
    amount_minor: int = Field(description="Paisa. Negative corrects an earlier fee.")
    reason: str = Field(min_length=1, max_length=500)


class RefundView(BaseModel):
    id: str
    booking_reference: str
    customer_name: str | None = None
    amount: BookingMoney
    method: RefundMethod
    status: RefundStatus
    reason: str
    payout_reference: str | None = None
    rejection_reason: str | None = None
    requested_by_user_id: str | None = None
    requested_at: datetime
    approved_by_user_id: str | None = None
    approved_at: datetime | None = None
    paid_by_user_id: str | None = None
    paid_at: datetime | None = None
    rejected_by_user_id: str | None = None
    rejected_at: datetime | None = None


class RefundList(BaseModel):
    refunds: list[RefundView]
    total: int


class RequestRefundRequest(BaseModel):
    amount_minor: int = Field(gt=0, description="Paisa, at most what was received and not yet refunded.")
    method: RefundMethod
    reason: str = Field(min_length=1, max_length=500)


class RejectRefundRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class MarkRefundPaidRequest(BaseModel):
    payout_reference: str = Field(min_length=1, max_length=120)
    paid_at: datetime


class ProfitView(BaseModel):
    selling: BookingMoney
    fees_charged: BookingMoney = Field(description="Fees the customer paid on top of the fare (card fee).")
    supplier_charge: BookingMoney | None = Field(None, description="Null when not known.")
    supplier_charge_basis: Literal["recorded", "quoted", "not_known"]
    payment_fees: BookingMoney
    refunds: BookingMoney
    profit: BookingMoney | None = Field(None, description="Null = 'Profit: not known' (no real supplier cost).")
    missing: list[Literal["payment_fees"]] = Field(default_factory=list, description="Parts not recorded yet; shown as 'Not recorded'.")


class BookingFinanceView(BaseModel):
    reference: str
    profit: ProfitView
    supplier_charges: list[SupplierChargeView]
    payment_fees: list[PaymentFeeView]
    refunds: list[RefundView]
    refundable: BookingMoney = Field(description="Received and not yet refunded (card fees excluded: they are not refunded).")
