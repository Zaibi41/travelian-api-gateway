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


class SupplierWalletView(BaseModel):
    provider_id: str
    name: str
    mode: Literal["api", "calculated"] = Field(description="api: read from the supplier; calculated: top-ups + adjustments - ticket charges.")
    balance: BookingMoney | None = Field(None, description="Null = not known yet (never read, or nothing recorded).")
    as_of: datetime | None = Field(None, description="When the supplier was last read (api) or the last movement (calculated).")
    tracking_since: datetime | None = None
    threshold: BookingMoney | None = Field(None, description="Alert below this. Null = no alert set.")
    low: bool | None = None
    note: str | None = Field(None, description="Plain words, e.g. why the balance can't be read.")


class SupplierWalletList(BaseModel):
    wallets: list[SupplierWalletView]


class WalletEntryRequest(BaseModel):
    amount_minor: int = Field(description="Paisa. Top-up: more than 0. Adjustment: + or -, never 0.")
    reference: str | None = Field(None, max_length=120)
    reason: str | None = Field(None, max_length=500)
    occurred_at: datetime


class WalletThresholdRequest(BaseModel):
    threshold_minor: int | None = Field(None, ge=0, le=100_000_000_000, description="Null removes the alert.")


class WalletHistoryItem(BaseModel):
    at: datetime
    kind: Literal["balance_check", "top_up", "adjustment", "ticket"]
    amount: BookingMoney | None = None
    balance: BookingMoney | None = None
    reference: str | None = None
    reason: str | None = None
    booking_reference: str | None = None
    by_user_id: str | None = None


class WalletHistory(BaseModel):
    items: list[WalletHistoryItem]


class ReportTotals(BaseModel):
    bookings: int
    sales: BookingMoney
    fees_charged: BookingMoney
    supplier_charge: BookingMoney | None = Field(None, description="Cost and margin permission only. Of bookings whose profit is known.")
    payment_fees: BookingMoney
    refunds: BookingMoney
    profit: BookingMoney | None = Field(None, description="Cost and margin permission only. Sum over bookings whose profit is known.")
    profit_not_known: int = Field(0, description="Sales whose supplier cost isn't known: left out of profit, never counted as 0.")
    payment_fees_missing: int = Field(0, description="Sales with a card payment whose fee isn't recorded yet.")
    other_currency: int = Field(0, description="Sales not in PKR, left out of the money totals.")


class ReportPeriod(ReportTotals):
    start: date


class ReceivableRow(BaseModel):
    booking_reference: str
    customer: str | None = None
    status: str
    total: BookingMoney
    due: BookingMoney
    hold_expires_at: datetime | None = None


class SupplierSpendRow(BaseModel):
    day: date
    supplier: str | None = Field(None, description="Supplier names permission only.")
    ticket_charges: BookingMoney
    top_ups: BookingMoney


class MethodRow(BaseModel):
    method: str
    account: str | None = None
    count: int
    amount: BookingMoney
    card_fees: BookingMoney


class BookingProfitRow(BaseModel):
    booking_reference: str
    created_at: datetime
    product: str
    customer: str | None = None
    sales: BookingMoney
    supplier_charge: BookingMoney | None = None
    supplier_charge_basis: Literal["recorded", "quoted", "not_known"]
    payment_fees: BookingMoney
    refunds: BookingMoney
    profit: BookingMoney | None = None


class FinanceReport(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    granularity: Literal["day", "week", "month"]
    totals: ReportTotals
    periods: list[ReportPeriod]
    by_booking: list[BookingProfitRow] | None = Field(None, description="Profit by booking (cost and margin permission only), largest sales first.")
    receivables: list[ReceivableRow]
    receivables_total: BookingMoney
    refunds_due: BookingMoney = Field(description="Refundable money on paid bookings that can't travel, plus refunds approved but not paid.")
    refunds_due_count: int
    supplier_spend: list[SupplierSpendRow] | None = Field(None, description="Cost and margin permission only.")
    payments_by_method: list[MethodRow]


class StatementPreviewRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2_200_000, description="The CSV file's text.")


class StatementPreview(BaseModel):
    columns: list[str]
    sample: list[list[str]] = Field(description="The first rows, to choose the columns.")
    rows: int
    date_formats: list[str]


class StatementColumns(BaseModel):
    reference: str | None = None
    description: str | None = None
    date: str | None = None
    amount: str | None = None
    credit: str | None = None
    debit: str | None = None
    fee: str | None = None
    net: str | None = None
    payout_date: str | None = None


class StatementImportRequest(StatementPreviewRequest):
    kind: Literal["payfast", "bank", "supplier"]
    account_id: str | None = Field(None, max_length=36, description="Bank: the receiving account the statement belongs to.")
    provider_id: Literal["southair", "sooperfare"] | None = Field(None, description="Supplier statements.")
    columns: StatementColumns
    date_format: Literal["DD/MM/YYYY", "MM/DD/YYYY", "YYYY-MM-DD", "DD-MMM-YYYY"] = "DD/MM/YYYY"


class StatementImportResult(BaseModel):
    import_id: str
    lines: int
    matched: int
    suggested: int
    unmatched: int
    mismatches_added: int
    fees_recorded: int = Field(description="PayFast fees taken from the settlement file onto payments (used in profit).")


class StatementImportView(BaseModel):
    id: str
    kind: Literal["payfast", "bank", "supplier"]
    file_name: str
    row_count: int
    imported_at: datetime
    imported_by_user_id: str | None = None
    account_label: str | None = None
    provider_id: str | None = None
    matched: int
    suggested: int
    unmatched: int


class StatementImportList(BaseModel):
    imports: list[StatementImportView]


class StatementLineView(BaseModel):
    id: str
    line_no: int
    reference: str | None = None
    description: str | None = None
    occurred_on: date | None = None
    amount: BookingMoney
    fee: BookingMoney | None = None
    net: BookingMoney | None = None
    payout_on: date | None = None
    status: Literal["matched", "suggested", "unmatched", "ignored"]
    booking_reference: str | None = None
    payment_reference: str | None = None
    payment_amount: BookingMoney | None = None


class StatementLineList(BaseModel):
    lines: list[StatementLineView]


class MismatchView(BaseModel):
    id: str
    kind: str
    detail: str
    booking_reference: str | None = None
    expected: BookingMoney | None = None
    actual: BookingMoney | None = None
    status: Literal["open", "explained", "resolved"]
    explanation: str | None = None
    explained_by_user_id: str | None = None
    explained_at: datetime | None = None
    resolution_reason: str | None = None
    resolved_by_user_id: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class MismatchList(BaseModel):
    mismatches: list[MismatchView]
    total: int


class ResolveMismatchRequest(BaseModel):
    status: Literal["explained", "resolved"]
    reason: str = Field(min_length=1, max_length=1000)


LedgerType = Literal["asset", "liability", "equity", "income", "expense"]


class LedgerAccountView(BaseModel):
    code: str
    name: str
    type: LedgerType
    system: bool = Field(description="Automatic journals post here: it can be renamed, not deactivated.")
    receiving_account: bool = False
    active: bool


class LedgerAccountList(BaseModel):
    accounts: list[LedgerAccountView]


class AddLedgerAccountRequest(BaseModel):
    code: str = Field(pattern=r"^[0-9]{4,10}$")
    name: str = Field(min_length=1, max_length=120)
    type: LedgerType


class UpdateLedgerAccountRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    active: bool | None = None


class JournalLineView(BaseModel):
    account_code: str
    account_name: str
    debit: BookingMoney
    credit: BookingMoney
    memo: str | None = None


class JournalView(BaseModel):
    id: str
    number: int
    posted_on: date
    description: str
    source_type: str
    booking_reference: str | None = None
    status: Literal["posted", "pending_approval", "rejected"]
    created_by_user_id: str | None = None
    approved_by_user_id: str | None = None
    approved_at: datetime | None = None
    rejected_reason: str | None = None
    lines: list[JournalLineView]


class JournalList(BaseModel):
    journals: list[JournalView]
    total: int


class ManualJournalLine(BaseModel):
    account_code: str = Field(pattern=r"^[0-9]{4,10}$")
    debit_minor: int = Field(0, ge=0)
    credit_minor: int = Field(0, ge=0)
    memo: str | None = Field(None, max_length=200)


class ManualJournalRequest(BaseModel):
    posted_on: date
    description: str = Field(min_length=1, max_length=300)
    lines: list[ManualJournalLine] = Field(min_length=2, max_length=50)


class RejectJournalRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AccountBalance(BaseModel):
    code: str
    name: str
    type: LedgerType
    debit: BookingMoney
    credit: BookingMoney
    balance: BookingMoney = Field(description="On the account's normal side (assets and expenses: debit - credit; others: credit - debit).")


class TrialBalance(BaseModel):
    date_to: date | None = None
    accounts: list[AccountBalance]
    total_debit: BookingMoney
    total_credit: BookingMoney
    balanced: bool


class ProfitAndLoss(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    income: list[AccountBalance]
    expenses: list[AccountBalance]
    total_income: BookingMoney
    total_expenses: BookingMoney
    net_profit: BookingMoney


class AccountEntry(BaseModel):
    number: int
    posted_on: date
    description: str
    source_type: str
    booking_reference: str | None = None
    debit: BookingMoney
    credit: BookingMoney
    balance: BookingMoney
    memo: str | None = None


class AccountStatement(BaseModel):
    code: str
    name: str
    type: LedgerType
    opening: BookingMoney
    entries: list[AccountEntry]
    closing: BookingMoney


class LedgerGap(BaseModel):
    source_type: str
    booking_reference: str | None = None
    reason: str
    detail: str
    created_at: datetime


class LedgerGapList(BaseModel):
    gaps: list[LedgerGap]


class LedgerPostResult(BaseModel):
    journals: int
    gaps: int
