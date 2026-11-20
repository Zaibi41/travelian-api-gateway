"""Finance API (travelian#91 and the Finance issues). Money is integer paisa (amount_minor) with its currency."""

from __future__ import annotations

from datetime import date, datetime
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
    profit: ProfitView | None = Field(None, description="Only with the cost and margin permission (left out otherwise).")
    supplier_charges: list[SupplierChargeView] | None = Field(None, description="Only with the cost and margin permission.")
    payment_fees: list[PaymentFeeView]
    refunds: list[RefundView]
    refundable: BookingMoney = Field(description="Received and not yet refunded (card fees excluded: they are not refunded).")


PaymentStatus = Literal["awaiting_check", "confirmed", "rejected"]
PaymentMethod = Literal["bank_transfer", "jazzcash", "1link", "payfast"]


class FinancePaymentRow(BaseModel):
    kind: Literal["payment", "slip"] = Field(description="payment: money recorded; slip: a customer's slip still awaiting a check, or rejected.")
    id: str
    booking_reference: str
    pnr: str | None = None
    product: str
    is_test: bool = False
    customer_name: str | None = None
    method: PaymentMethod
    account_id: str | None = None
    account_label: str | None = None
    chosen_account_id: str | None = Field(None, description="The account the customer chose at checkout.")
    amount: BookingMoney
    card_fee: BookingMoney = Field(description="Card fee the customer paid on top (travelian#74).")
    payment_fee: BookingMoney | None = Field(None, description="What the payment cost Travelian. Null = not recorded yet.")
    payer_reference: str | None = None
    status: PaymentStatus
    at: datetime = Field(description="Received (payment) or submitted (slip).")
    slip_id: str | None = None
    rejection_reason: str | None = None


class PaymentDayTotal(BaseModel):
    day: date
    method: PaymentMethod
    count: int
    amount: BookingMoney
    card_fee: BookingMoney


class FinancePaymentList(BaseModel):
    payments: list[FinancePaymentRow]
    total: int
    confirmed: BookingMoney
    confirmed_count: int
    awaiting: BookingMoney
    awaiting_count: int
    daily_totals: list[PaymentDayTotal]


class SlipLink(BaseModel):
    url: str = Field(description="Signed link to the private file; expires.")
    content_type: str
    expires_in_seconds: int


class ConfirmSlipRequest(BaseModel):
    amount_minor: int = Field(gt=0, description="What actually arrived (paisa).")
    account_id: str = Field(max_length=36, description="The receiving account it arrived in.")
    payer_reference: str | None = Field(None, max_length=64)
    received_at: datetime


class RejectSlipRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class SlipView(BaseModel):
    id: str
    booking_reference: str
    method: Literal["bank_transfer", "jazzcash"]
    account_id: str | None = None
    account_label: str | None = None
    amount: BookingMoney
    payer_reference: str | None = None
    content_type: str
    status: PaymentStatus
    submitted_via: Literal["checkout", "my_bookings"]
    submitted_at: datetime
    reviewed_by_user_id: str | None = None
    reviewed_at: datetime | None = None
    rejection_reason: str | None = None
    payment_id: str | None = None


class RefundDue(BaseModel):
    booking_reference: str
    pnr: str | None = None
    product: str
    booking_status: str
    is_test: bool = False
    customer_name: str | None = None
    reason: str | None = Field(None, description="Why the booking couldn't be travelled on (e.g. hold_expired), when recorded.")
    paid: BookingMoney
    refundable: BookingMoney


class RefundsDueView(BaseModel):
    owed: list[RefundDue] = Field(description="Paid bookings with no refund recorded yet.")
    open: list[RefundView] = Field(description="Refunds requested or approved, not paid yet.")


class PaymentSettings(BaseModel):
    card_fee_bps: int = Field(description="Card / 1Link fee in basis points (300 = 3%).")
    card_fee_on_group: bool
    card_fee_refunded: bool = Field(False, description="Always false: the fee is not refunded (owner, 20 Nov 2026).")


class UpdatePaymentSettings(BaseModel):
    card_fee_bps: int | None = Field(None, ge=0, le=1000)
    card_fee_on_group: bool | None = None
    reason: str | None = Field(None, max_length=500)
