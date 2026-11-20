"""Finance (travelian#91): audit log, per-booking money (profit, supplier charges, payment fees, refunds).

Every route needs the Finance permission (app.core.field_visibility.require_finance): Super Admin always, staff when
granted; agency and customer roles get 403. Nothing here moves money or calls a supplier.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from app.core.authentication import AuthenticatedUser, require_authenticated_user
from app.core.field_visibility import FINANCE, BackOfficeUser, _back_office_user, require_finance
from app.db import finance as store
from app.schemas.booking_common import BookingMoney
from app.schemas.finance import (
    AddPaymentFeeRequest, AddSupplierChargeRequest, AuditEntryView, AuditLogPage, BookingFinanceView, FinanceAccess, MarkRefundPaidRequest,
    PaymentFeeView, ProfitView, RefundList, RefundView, RejectRefundRequest, RequestRefundRequest, SupplierChargeView,
)
from app.services.finance_profit import ProfitInputs, calculate

PKT = ZoneInfo("Asia/Karachi")

router = APIRouter(prefix="/admin/finance", tags=["admin finance"], responses={
    403: {"description": "Caller doesn't have the Finance permission."}, 503: {"description": "Database not available."}})


def actor(viewer: BackOfficeUser) -> store.Actor:
    return store.Actor(viewer.user_id, viewer.role)


def refused(error: store.FinanceRefused) -> HTTPException:
    return HTTPException(error.status, error.message)


def day_range(date_from: date | None, date_to: date | None) -> tuple[datetime | None, datetime | None]:
    """Whole days in Pakistan time: from 00:00 of date_from to 00:00 after date_to."""
    start = datetime.combine(date_from, time(), PKT) if date_from else None
    end = datetime.combine(date_to + timedelta(days=1), time(), PKT) if date_to else None
    return start, end


def money(amount: int, currency: str) -> BookingMoney:
    return BookingMoney(amount_minor=amount, currency=currency)


@router.get("/access", response_model=FinanceAccess, responses={403: {"description": "Not back office."}})
async def finance_access(user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)]) -> FinanceAccess:
    """Whether the caller may open Finance screens (the menu uses it; the gateway still checks every call)."""
    viewer = await run_in_threadpool(_back_office_user, user)
    return FinanceAccess(finance=viewer.can(FINANCE))


@router.get("/audit-log", response_model=AuditLogPage)
async def audit_log(viewer: Annotated[BackOfficeUser, Depends(require_finance)],
                    action: str | None = Query(None, max_length=64), entity_type: str | None = Query(None, max_length=40),
                    booking_reference: str | None = Query(None, max_length=64),
                    date_from: date | None = None, date_to: date | None = None,
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> AuditLogPage:
    """Every finance action, newest first. Read-only. Dates are whole days in Pakistan time."""
    start, end = day_range(date_from, date_to)
    entries, total = await run_in_threadpool(lambda: store.list_audit(action=action, entity_type=entity_type, booking_reference=booking_reference,
                                                                      date_from=start, date_to=end, limit=limit, offset=offset))
    return AuditLogPage(entries=[AuditEntryView(**entry.__dict__) for entry in entries], total=total)


def _charge(c: store.SupplierCharge) -> SupplierChargeView:
    return SupplierChargeView(id=c.id, traveler_position=c.traveler_position, traveler_name=c.traveler_name, amount=money(c.amount_minor, c.currency),
                              source=c.source, supplier_reference=c.supplier_reference, reason=c.reason,
                              recorded_by_user_id=c.recorded_by_user_id, recorded_at=c.recorded_at)


def _fee(f: store.PaymentFee) -> PaymentFeeView:
    return PaymentFeeView(id=f.id, payment_id=f.payment_id, amount=money(f.amount_minor, f.currency), source=f.source, reason=f.reason,
                          recorded_by_user_id=f.recorded_by_user_id, recorded_at=f.recorded_at)


def refund_view(r: store.Refund) -> RefundView:
    data = {k: v for k, v in r.__dict__.items() if k not in ("amount_minor", "currency")}
    return RefundView(**data, amount=money(r.amount_minor, r.currency))


def profit_inputs(data: store.BookingFinance) -> ProfitInputs:
    fees_by_payment: dict[str, int] = {}
    for fee in data.fees:
        fees_by_payment[fee.payment_id] = fees_by_payment.get(fee.payment_id, 0) + fee.amount_minor
    return ProfitInputs(
        selling_minor=data.booking.total_minor, stored_cost_minor=data.booking.supplier_cost_minor, cost_known=data.booking.cost_known,
        fees_charged_minor=sum(p.get("card_fee_minor") or 0 for p in data.payments),
        supplier_charges_minor=[c.amount_minor for c in data.charges], payment_fees_minor=[f.amount_minor for f in data.fees],
        refunds_paid_minor=[r.amount_minor for r in data.refunds if r.status == "paid"],
        card_payments_without_fee=sum(1 for p in data.payments if p["method"] == "payfast" and p["id"] not in fees_by_payment),
    )


def profit_view(data: store.BookingFinance) -> ProfitView:
    currency = data.booking.currency
    p = calculate(profit_inputs(data))
    return ProfitView(selling=money(p.selling_minor, currency), fees_charged=money(p.fees_charged_minor, currency),
                      supplier_charge=None if p.supplier_charge_minor is None else money(p.supplier_charge_minor, currency),
                      supplier_charge_basis=p.supplier_charge_basis, payment_fees=money(p.payment_fees_minor, currency),
                      refunds=money(p.refunds_minor, currency), profit=None if p.profit_minor is None else money(p.profit_minor, currency),
                      missing=list(p.missing))


async def _booking_view(reference: str) -> BookingFinanceView:
    try:
        data = await run_in_threadpool(store.booking_finance, reference)
    except store.FinanceRefused as error:
        raise refused(error) from None
    return BookingFinanceView(reference=data.booking.reference, profit=profit_view(data), supplier_charges=[_charge(c) for c in data.charges],
                              payment_fees=[_fee(f) for f in data.fees], refunds=[refund_view(r) for r in data.refunds],
                              refundable=money(data.refundable_minor, data.booking.currency))


@router.get("/bookings/{reference}", response_model=BookingFinanceView, responses={404: {"description": "Booking not found."}})
async def booking_finance(reference: str, viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> BookingFinanceView:
    """A booking's profit (selling + fees charged - supplier charge - payment fees - refunds), supplier charges, fees and refunds."""
    return await _booking_view(reference)


@router.post("/bookings/{reference}/supplier-charges", response_model=BookingFinanceView, status_code=201,
             responses={404: {"description": "Booking not found."}, 409: {"description": "Charges would go below zero."},
                        422: {"description": "Amount zero, reason missing or unknown traveler."}})
async def add_supplier_charge(reference: str, body: AddSupplierChargeRequest,
                              viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> BookingFinanceView:
    """Record what the supplier actually charged (tickets issued in a supplier portal), or correct it with a negative amount. Audited."""
    try:
        await run_in_threadpool(lambda: store.add_supplier_charge(reference, actor(viewer), amount_minor=body.amount_minor, reason=body.reason,
                                                                  traveler_position=body.traveler_position,
                                                                  supplier_reference=body.supplier_reference))
    except store.FinanceRefused as error:
        raise refused(error) from None
    return await _booking_view(reference)


@router.post("/payments/{payment_id}/fees", response_model=PaymentFeeView, status_code=201,
             responses={404: {"description": "Payment not found."}, 409: {"description": "Fees would go below zero."}})
async def add_payment_fee(payment_id: str, body: AddPaymentFeeRequest, viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> PaymentFeeView:
    """Record what a payment cost Travelian (e.g. the PayFast fee from its statement), or correct it. Audited."""
    try:
        fee = await run_in_threadpool(lambda: store.add_payment_fee(payment_id, actor(viewer), amount_minor=body.amount_minor, reason=body.reason))
    except store.FinanceRefused as error:
        raise refused(error) from None
    return _fee(fee)


@router.get("/refunds", response_model=RefundList)
async def list_refunds(viewer: Annotated[BackOfficeUser, Depends(require_finance)],
                       status: str | None = Query(None, pattern="^(requested|approved|paid|rejected)$"),
                       limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> RefundList:
    refunds, total = await run_in_threadpool(lambda: store.list_refunds(status, limit, offset))
    return RefundList(refunds=[refund_view(r) for r in refunds], total=total)


@router.post("/bookings/{reference}/refunds", response_model=RefundView, status_code=201,
             responses={404: {"description": "Booking not found."}, 409: {"description": "More than can be refunded."}})
async def request_refund(reference: str, body: RequestRefundRequest, viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> RefundView:
    """Record that a customer is owed a refund (status requested). Moves no money. Audited."""
    try:
        refund = await run_in_threadpool(lambda: store.request_refund(reference, actor(viewer), amount_minor=body.amount_minor, method=body.method,
                                                                      reason=body.reason))
    except store.FinanceRefused as error:
        raise refused(error) from None
    return refund_view(refund)


@router.post("/refunds/{refund_id}/approve", response_model=RefundView, responses={404: {"description": "Refund not found."},
                                                                                  409: {"description": "Not in 'requested'."}})
async def approve_refund(refund_id: str, viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> RefundView:
    try:
        return refund_view(await run_in_threadpool(store.approve_refund, refund_id, actor(viewer)))
    except store.FinanceRefused as error:
        raise refused(error) from None


@router.post("/refunds/{refund_id}/reject", response_model=RefundView, responses={404: {"description": "Refund not found."},
                                                                                 409: {"description": "Already paid or rejected."}})
async def reject_refund(refund_id: str, body: RejectRefundRequest, viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> RefundView:
    try:
        return refund_view(await run_in_threadpool(store.reject_refund, refund_id, actor(viewer), body.reason))
    except store.FinanceRefused as error:
        raise refused(error) from None


@router.post("/refunds/{refund_id}/paid", response_model=RefundView, responses={404: {"description": "Refund not found."},
                                                                               409: {"description": "Not approved."}})
async def mark_refund_paid(refund_id: str, body: MarkRefundPaidRequest, viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> RefundView:
    """Record a payout staff already made (bank, JazzCash or the PayFast portal). The gateway sends no money."""
    try:
        return refund_view(await run_in_threadpool(lambda: store.mark_refund_paid(refund_id, actor(viewer), payout_reference=body.payout_reference,
                                                                                  paid_at=body.paid_at)))
    except store.FinanceRefused as error:
        raise refused(error) from None
