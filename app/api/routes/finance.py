"""Finance (travelian#91): audit log, per-booking money (profit, supplier charges, payment fees, refunds).

Every route needs the Finance permission (app.core.field_visibility.require_finance): Super Admin always, staff when
granted; agency and customer roles get 403. Nothing here moves money or calls a supplier.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
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
    views = []
    for entry in entries:
        view = AuditEntryView(**entry.__dict__)
        if hides_cost(entry.entity_type, entry.entity_id) and not viewer.visibility.cost_and_margin:
            view.before = view.after = None
        views.append(view)
    return AuditLogPage(entries=views, total=total)


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
        **_finance_ops_inputs(data.booking.id),
    )


def _finance_ops_inputs(booking_id: str) -> dict:
    """D57 accepted difference and issue #202 supplier billable amount ({} before migration 0037 or when unreadable)."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.db import finance_ops, online_attempts

    try:
        with store.agencies.application_engine().begin() as connection:
            if not online_attempts.has_finance_ops(connection):
                return {}
            found = finance_ops.shortfall(booking_id, connection)
            billing = finance_ops.billing(booking_id, connection)
    except (HTTPException, SQLAlchemyError):  # supplementary: the booking's finance view still opens
        return {}
    return {"shortfall_minor": found.amount_minor if found else 0, "billable_minor": billing.billable_minor if billing else None}


def profit_view(data: store.BookingFinance) -> ProfitView:
    currency = data.booking.currency
    p = calculate(profit_inputs(data))
    return ProfitView(selling=money(p.selling_minor, currency), fees_charged=money(p.fees_charged_minor, currency),
                      supplier_charge=None if p.supplier_charge_minor is None else money(p.supplier_charge_minor, currency),
                      supplier_charge_basis=p.supplier_charge_basis, payment_fees=money(p.payment_fees_minor, currency),
                      refunds=money(p.refunds_minor, currency), profit=None if p.profit_minor is None else money(p.profit_minor, currency),
                      shortfall=money(p.shortfall_minor, currency) if p.shortfall_minor else None, missing=list(p.missing))


#: Audit entries whose before/after carry supplier cost; sent only with the cost and margin permission.
COST_ENTITIES = {"supplier_charge", "supplier_wallet"}


def hides_cost(entity_type: str, entity_id: str | None) -> bool:
    """Audit entries whose before/after carry supplier cost or balances (incl. low-balance alert levels)."""
    return entity_type in COST_ENTITIES or (entity_type == "finance_setting" and (entity_id or "").startswith("low_balance_"))


async def _booking_view(reference: str, viewer: BackOfficeUser) -> BookingFinanceView:
    try:
        data = await run_in_threadpool(store.booking_finance, reference)
    except store.FinanceRefused as error:
        raise refused(error) from None
    view = BookingFinanceView(reference=data.booking.reference, payment_fees=[_fee(f) for f in data.fees], refunds=[refund_view(r) for r in data.refunds],
                              refundable=money(data.refundable_minor, data.booking.currency))
    if viewer.visibility.cost_and_margin:  # supplier cost and profit: never without the grant (AGENTS.md field visibility)
        view.profit = profit_view(data)
        view.supplier_charges = [_charge(c) for c in data.charges]
    return view


def require_cost(viewer: BackOfficeUser) -> None:
    if not viewer.visibility.cost_and_margin:
        raise HTTPException(403, "Recording supplier charges needs the cost and margin permission.")


@router.get("/bookings/{reference}", response_model=BookingFinanceView, responses={404: {"description": "Booking not found."}})
async def booking_finance(reference: str, viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> BookingFinanceView:
    """A booking's profit (selling + fees charged - supplier charge - payment fees - refunds), supplier charges, fees and refunds."""
    return await _booking_view(reference, viewer)


@router.post("/bookings/{reference}/supplier-charges", response_model=BookingFinanceView, status_code=201,
             responses={404: {"description": "Booking not found."}, 409: {"description": "Charges would go below zero."},
                        422: {"description": "Amount zero, reason missing or unknown traveler."}})
async def add_supplier_charge(reference: str, body: AddSupplierChargeRequest,
                              viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> BookingFinanceView:
    """Record what the supplier actually charged (tickets issued in a supplier portal), or correct it with a negative amount. Audited.
    Needs the cost and margin permission too."""
    require_cost(viewer)
    try:
        await run_in_threadpool(lambda: store.add_supplier_charge(reference, actor(viewer), amount_minor=body.amount_minor, reason=body.reason,
                                                                  traveler_position=body.traveler_position,
                                                                  supplier_reference=body.supplier_reference))
    except store.FinanceRefused as error:
        raise refused(error) from None
    return await _booking_view(reference, viewer)


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


# ------------------------------------------------------------------------------------------------ Finance > Payments (#69)
from fastapi.responses import Response  # noqa: E402

from app.core.field_visibility import RECORD_PAYMENTS, require_back_office  # noqa: E402
from app.db import finance_payments, payment_slips  # noqa: E402
from app.schemas.finance import (  # noqa: E402
    ConfirmSlipRequest, FinancePaymentList, FinancePaymentRow, PaymentDayTotal, PaymentSettings, RefundDue, RefundsDueView, RejectSlipRequest,
    SlipLink, SlipView, UpdatePaymentSettings,
)
from app.services import card_fee, payment_proof  # noqa: E402

STATUS_PATTERN = "^(awaiting_check|confirmed|rejected)$"
METHOD_PATTERN = "^(bank_transfer|jazzcash|1link|payfast)$"


def _payment_query(date_from, date_to, method, status, account_id, q) -> finance_payments.PaymentQuery:
    start, end = day_range(date_from, date_to)
    return finance_payments.PaymentQuery(start, end, method, status, account_id, q)


def _row(r: dict) -> FinancePaymentRow:
    c = r["currency"]
    return FinancePaymentRow(
        kind=r["kind"], id=r["id"], booking_reference=r["booking_reference"], pnr=r["pnr"], product=r["product"], is_test=bool(r["is_test"]),
        customer_name=r["customer_name"], method=r["method"], account_id=r["account_id"], account_label=r["account_label"],
        chosen_account_id=r["chosen_account_id"], amount=money(r["amount_minor"], c), card_fee=money(r["card_fee_minor"], c),
        payment_fee=money(r["payment_fee_minor"], c) if r["fee_recorded"] else None, payer_reference=r["payer_reference"],
        status=r["status"], at=r["at"], slip_id=r["slip_id"], rejection_reason=r["rejection_reason"])


@router.get("/payments", response_model=FinancePaymentList)
async def list_payments(viewer: Annotated[BackOfficeUser, Depends(require_finance)],
                        date_from: date | None = None, date_to: date | None = None,
                        method: str | None = Query(None, pattern=METHOD_PATTERN), status: str | None = Query(None, pattern=STATUS_PATTERN),
                        account_id: str | None = Query(None, max_length=36), q: str | None = Query(None, max_length=80),
                        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> FinancePaymentList:
    """Every payment across bookings (and slips awaiting a check or rejected), newest first, with totals per day and method
    (confirmed money only). Dates are whole days in Pakistan time; default All."""
    query = _payment_query(date_from, date_to, method, status, account_id, q)
    page = await run_in_threadpool(finance_payments.list_payments, query, limit, offset)
    return FinancePaymentList(
        payments=[_row(r) for r in page.rows], total=page.total, confirmed=money(page.confirmed_minor, "PKR"), confirmed_count=page.confirmed_count,
        awaiting=money(page.awaiting_minor, "PKR"), awaiting_count=page.awaiting_count,
        daily_totals=[PaymentDayTotal(day=d.day, method=d.method, count=d.count, amount=money(d.amount_minor, "PKR"),
                                      card_fee=money(d.card_fee_minor, "PKR")) for d in page.days])


class Number(str):
    """A cell the gateway built as a number (money): written as is, never escaped."""


def rupees(minor: int | None) -> str:
    """Integer paisa as a rupee string with 2 decimals (CSV); exact, no floats."""
    if minor is None:
        return ""
    sign, value = ("-", -minor) if minor < 0 else ("", minor)
    return Number(f"{sign}{value // 100}.{value % 100:02d}")


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


METHOD_LABEL = {"bank_transfer": "Bank transfer", "jazzcash": "JazzCash", "1link": "1Link", "payfast": "Card (PayFast)", "alfalah": "Card (Bank Alfalah)"}
STATUS_LABEL = {"awaiting_check": "Awaiting check", "confirmed": "Confirmed", "rejected": "Rejected"}


def csv_text(header: list[str], rows: list[list[Any]]) -> str:
    import csv
    import io

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        # Text starting with = + - @, tab or CR could run as a formula in Excel: prefix it with a quote (money cells are Number).
        writer.writerow([f"'{v}" if isinstance(v, str) and not isinstance(v, Number) and v.startswith(FORMULA_START) else v for v in row])
    return out.getvalue()


@router.get("/payments.csv", response_class=Response, responses={200: {"content": {"text/csv": {}}}})
async def export_payments(viewer: Annotated[BackOfficeUser, Depends(require_finance)],
                          date_from: date | None = None, date_to: date | None = None,
                          method: str | None = Query(None, pattern=METHOD_PATTERN), status: str | None = Query(None, pattern=STATUS_PATTERN),
                          account_id: str | None = Query(None, max_length=36), q: str | None = Query(None, max_length=80)) -> Response:
    """The same list as /payments with the same filters, as CSV (amounts in rupees with paisa). Audited as an export."""
    query = _payment_query(date_from, date_to, method, status, account_id, q)
    rows = await run_in_threadpool(finance_payments.export_rows, query)
    body = csv_text(
        ["Date (PKT)", "Booking", "PNR", "Customer", "Method", "Account", "Amount (Rs)", "Card fee (Rs)", "Payment fee (Rs)", "Reference",
         "Status", "Test booking"],
        [[r["at"].astimezone(PKT).strftime("%Y-%m-%d %H:%M"), r["booking_reference"], r["pnr"] or "", r["customer_name"] or "",
          METHOD_LABEL.get(r["method"], r["method"]), r["account_label"] or "", rupees(r["amount_minor"]), rupees(r["card_fee_minor"]),
          rupees(r["payment_fee_minor"]) if r["fee_recorded"] else "Not recorded", r["payer_reference"] or "", STATUS_LABEL[r["status"]],
          "Yes" if r["is_test"] else ""] for r in rows])

    def log() -> None:
        with store.agencies.application_engine().begin() as connection:
            store.audit(connection, actor(viewer), "payments_exported", "export", None,
                        after={"rows": len(rows), "date_from": date_from, "date_to": date_to, "method": method, "status": status})
    await run_in_threadpool(log)
    name = f"travelian-payments-{date_from or 'all'}-{date_to or 'all'}.csv"
    return Response(body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _slip_view(s: payment_slips.Slip) -> SlipView:
    return SlipView(id=s.id, booking_reference=s.booking_reference, method=s.method, account_id=s.account_id, account_label=s.account_label,
                    amount=money(s.amount_minor, s.currency), payer_reference=s.payer_reference, content_type=s.content_type, status=s.status,
                    submitted_via=s.submitted_via, submitted_at=s.submitted_at, reviewed_by_user_id=s.reviewed_by_user_id,
                    reviewed_at=s.reviewed_at, rejection_reason=s.rejection_reason, payment_id=s.payment_id)


def _slip_refused(error: payment_slips.SlipRefused | payment_proof.ProofError) -> HTTPException:
    return HTTPException(error.status, error.message)


async def require_slip_viewer(viewer: Annotated[BackOfficeUser, Depends(require_back_office)]) -> BackOfficeUser:
    """Slips: Finance, or staff who may record payments (the booking's Payments tab)."""
    if not (viewer.can(FINANCE) or viewer.can(RECORD_PAYMENTS)):
        raise HTTPException(403, "Payment slips need the Finance or record payments permission.")
    return viewer


@router.get("/slips/{slip_id}", response_model=SlipView, responses={404: {}})
async def get_slip(slip_id: str, viewer: Annotated[BackOfficeUser, Depends(require_slip_viewer)]) -> SlipView:
    try:
        return _slip_view(await run_in_threadpool(payment_slips.get, slip_id))
    except payment_slips.SlipRefused as error:
        raise _slip_refused(error) from None


@router.get("/slips/{slip_id}/link", response_model=SlipLink, responses={404: {}, 503: {}})
async def slip_link(slip_id: str, viewer: Annotated[BackOfficeUser, Depends(require_slip_viewer)]) -> SlipLink:
    """A signed link to the private slip file, valid for 5 minutes."""
    try:
        slip = await run_in_threadpool(payment_slips.get, slip_id)
        url = await run_in_threadpool(payment_proof.signed_link, slip.asset_key)
    except (payment_slips.SlipRefused, payment_proof.ProofError) as error:
        raise _slip_refused(error) from None
    return SlipLink(url=url, content_type=slip.content_type, expires_in_seconds=payment_proof.LINK_SECONDS)


@router.post("/slips/{slip_id}/confirm", response_model=SlipView, responses={404: {}, 409: {}, 422: {}})
async def confirm_slip(slip_id: str, body: ConfirmSlipRequest, viewer: Annotated[BackOfficeUser, Depends(require_slip_viewer)]) -> SlipView:
    """The money arrived: record the payment (amount, account and reference as received) and confirm the slip. Audited."""
    try:
        return _slip_view(await run_in_threadpool(lambda: payment_slips.confirm(
            slip_id, actor(viewer), amount_minor=body.amount_minor, account_id=body.account_id, payer_reference=body.payer_reference,
            received_at=body.received_at)))
    except payment_slips.SlipRefused as error:
        raise _slip_refused(error) from None


@router.post("/slips/{slip_id}/reject", response_model=SlipView, responses={404: {}, 409: {}, 422: {}})
async def reject_slip(slip_id: str, body: RejectSlipRequest, viewer: Annotated[BackOfficeUser, Depends(require_slip_viewer)]) -> SlipView:
    """The money didn't arrive (or the slip is wrong): reject it with a reason. Nothing is recorded as paid. Audited."""
    try:
        return _slip_view(await run_in_threadpool(payment_slips.reject, slip_id, actor(viewer), body.reason))
    except payment_slips.SlipRefused as error:
        raise _slip_refused(error) from None


@router.get("/refunds-due", response_model=RefundsDueView)
async def refunds_due(viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> RefundsDueView:
    """Paid bookings that can't be travelled on with no refund recorded yet, and refunds requested or approved."""
    owed = await run_in_threadpool(finance_payments.refunds_due)
    requested, _ = await run_in_threadpool(lambda: store.list_refunds("requested", 500, 0))
    approved, _ = await run_in_threadpool(lambda: store.list_refunds("approved", 500, 0))
    return RefundsDueView(
        owed=[RefundDue(booking_reference=r["booking_reference"], pnr=r["pnr"], product=r["product"], booking_status=r["booking_status"],
                        is_test=bool(r["is_test"]), customer_name=r["customer_name"], reason=r["reason"], paid=money(r["paid_minor"], r["currency"]),
                        refundable=money(r["refundable_minor"], r["currency"])) for r in owed],
        open=[refund_view(r) for r in sorted(requested + approved, key=lambda r: r.requested_at)])


def _payment_settings(stored: dict) -> PaymentSettings:
    config = card_fee.settings_from(stored)
    return PaymentSettings(card_fee_bps=config["card_fee_bps"], card_fee_on_group=config["card_fee_on_group"])


@router.get("/settings/payments", response_model=PaymentSettings)
async def payment_settings(viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> PaymentSettings:
    return _payment_settings(await run_in_threadpool(store.get_settings))


@router.put("/settings/payments", response_model=PaymentSettings, responses={422: {}})
async def update_payment_settings(body: UpdatePaymentSettings, viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> PaymentSettings:
    """Change the card fee rate or whether it applies to group fares. Each change is audited; new checkouts use it."""
    stored: dict = {}
    for key in ("card_fee_bps", "card_fee_on_group"):
        value = getattr(body, key)
        if value is None:
            continue
        try:
            card_fee.validate(key, value)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        stored = await run_in_threadpool(store.set_setting, key, value, actor(viewer), body.reason)
    return _payment_settings(stored or await run_in_threadpool(store.get_settings))


# ------------------------------------------------------------------------------------------------ Finance > Wallets (#68)
from app.db import supplier_wallets as wallet_store  # noqa: E402
from app.schemas.finance import (  # noqa: E402
    SupplierWalletList, SupplierWalletView, WalletEntryRequest, WalletHistory, WalletHistoryItem, WalletThresholdRequest,
)
from app.services import supplier_wallets as wallet_service  # noqa: E402

PROVIDER_PATTERN = "^(southair|sooperfare)$"


async def require_wallet_viewer(viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> BackOfficeUser:
    """Supplier balances: Finance and the cost and margin permission (never agency roles)."""
    if not viewer.visibility.cost_and_margin:
        raise HTTPException(403, "Supplier balances need the cost and margin permission.")
    return viewer


def _wallet_view(w: wallet_store.Wallet, note: str | None = None) -> SupplierWalletView:
    if note is None and w.balance_minor is None:
        note = ("Not read yet. Press Refresh." if w.mode == "api"
                else "SooperFare has no balance API. Record a top-up (or the opening balance as an adjustment) to start.")
    return SupplierWalletView(provider_id=w.provider_id, name=w.name, mode=w.mode,
                              balance=None if w.balance_minor is None else money(w.balance_minor, w.currency), as_of=w.as_of,
                              tracking_since=w.tracking_since, threshold=None if w.threshold_minor is None else money(w.threshold_minor, w.currency),
                              low=w.low, note=note)


@router.get("/supplier-balances", response_model=SupplierWalletList)
async def supplier_balances(viewer: Annotated[BackOfficeUser, Depends(require_wallet_viewer)]) -> SupplierWalletList:
    """Each supplier's last known balance: Southair as last read from its API, SooperFare calculated. No customer wallets."""
    return SupplierWalletList(wallets=[_wallet_view(w) for w in await run_in_threadpool(wallet_store.wallets)])


@router.post("/supplier-balances/southair/refresh", response_model=SupplierWalletView, responses={502: {"description": "Southair didn't answer."}})
async def refresh_southair(viewer: Annotated[BackOfficeUser, Depends(require_wallet_viewer)]) -> SupplierWalletView:
    """Read Southair's balance now (read-only /my-profile) and keep it as a snapshot."""
    try:
        return _wallet_view(await wallet_service.refresh_southair(actor(viewer)))
    except wallet_service.BalanceUnavailable as error:
        raise HTTPException(502, error.message) from None


async def _entry(provider: str, kind: str, body: WalletEntryRequest, viewer: BackOfficeUser) -> SupplierWalletView:
    try:
        await run_in_threadpool(lambda: wallet_store.add_entry(provider, actor(viewer), kind=kind, amount_minor=body.amount_minor,
                                                               reference=body.reference, reason=body.reason, occurred_at=body.occurred_at))
    except store.FinanceRefused as error:
        raise refused(error) from None
    return _wallet_view(await wallet_service.check_low(provider))


@router.post("/supplier-balances/{provider}/top-ups", response_model=SupplierWalletView, status_code=201, responses={422: {}})
async def record_top_up(viewer: Annotated[BackOfficeUser, Depends(require_wallet_viewer)], body: WalletEntryRequest,
                        provider: str = PathParam(pattern=PROVIDER_PATTERN)) -> SupplierWalletView:
    """Record money Travelian paid into a supplier account with no balance API (SooperFare). Records only; moves no money."""
    return await _entry(provider, "top_up", body, viewer)


@router.post("/supplier-balances/{provider}/adjustments", response_model=SupplierWalletView, status_code=201, responses={422: {}})
async def adjust_balance(viewer: Annotated[BackOfficeUser, Depends(require_wallet_viewer)], body: WalletEntryRequest,
                         provider: str = PathParam(pattern=PROVIDER_PATTERN)) -> SupplierWalletView:
    """Correct the calculated balance (+ or -) with a reason, e.g. to match SooperFare's portal. Audited."""
    return await _entry(provider, "adjustment", body, viewer)


@router.put("/supplier-balances/{provider}/threshold", response_model=SupplierWalletView)
async def set_threshold(viewer: Annotated[BackOfficeUser, Depends(require_wallet_viewer)], body: WalletThresholdRequest,
                        provider: str = PathParam(pattern=PROVIDER_PATTERN)) -> SupplierWalletView:
    """The low-balance alert level for this supplier (a Finance setting, audited). Null removes it."""
    await run_in_threadpool(store.set_setting, wallet_store.threshold_key(provider), body.threshold_minor, actor(viewer))
    return _wallet_view(await wallet_service.check_low(provider))


@router.get("/supplier-balances/{provider}/history", response_model=WalletHistory)
async def wallet_history(viewer: Annotated[BackOfficeUser, Depends(require_wallet_viewer)],
                         provider: str = PathParam(pattern=PROVIDER_PATTERN)) -> WalletHistory:
    """Balance checks, top-ups, adjustments and ticket charges, newest first."""
    items = await run_in_threadpool(wallet_store.history, provider)
    return WalletHistory(items=[WalletHistoryItem(at=i.at, kind=i.kind, amount=None if i.amount_minor is None else money(i.amount_minor, "PKR"),
                                                  balance=None if i.balance_minor is None else money(i.balance_minor, "PKR"), reference=i.reference,
                                                  reason=i.reason, booking_reference=i.booking_reference, by_user_id=i.by_user_id) for i in items])


# ------------------------------------------------------------------------------------------------ Reports (#70)
from app.schemas.finance import (  # noqa: E402
    BookingProfitRow, FinanceReport, MethodRow, ReceivableRow, ReportPeriod, ReportTotals, SupplierSpendRow,
)
from app.services import finance_reports as reports, xlsx  # noqa: E402

GRANULARITY = "^(day|week|month)$"
PRODUCT = "^(flight|group|hotel|umrah)$"


def _totals_fields(t: reports.Totals, cost: bool) -> dict:
    pkr = lambda minor: money(minor, "PKR")  # noqa: E731
    return dict(bookings=t.bookings, sales=pkr(t.sales_minor), fees_charged=pkr(t.fees_charged_minor),
                supplier_charge=pkr(t.supplier_charge_minor) if cost else None, payment_fees=pkr(t.payment_fees_minor),
                refunds=pkr(t.refunds_minor), profit=pkr(t.profit_minor) if cost else None, profit_not_known=t.profit_not_known,
                payment_fees_missing=t.payment_fees_missing, other_currency=t.other_currency)


def build_report(viewer: BackOfficeUser, date_from: date | None, date_to: date | None, granularity: str, product: str | None) -> FinanceReport:
    from app.db.bookings import list_dashboard_bookings

    cost, names = viewer.visibility.cost_and_margin, viewer.visibility.supplier_names
    rows = [r for r in list_dashboard_bookings() if r.product != "tour" and r.product != "visa"]
    sales = reports.sales_in(rows, date_from, date_to, product)
    profit_map = reports.profits(sales)
    totals = reports.Totals()
    for row in sales:
        totals.add(row, profit_map[row.booking_id])
    open_rows = [r for r in rows if not product or r.product == product]
    paid = reports.paid_by_booking(r.booking_id for r in open_rows)
    owed = reports.receivables(open_rows, paid)
    start, end = day_range(date_from, date_to)
    due_rows = finance_payments.refunds_due()
    approved, _ = store.list_refunds("approved", 500, 0)
    requested, _ = store.list_refunds("requested", 500, 0)
    report = FinanceReport(
        date_from=date_from, date_to=date_to, granularity=granularity, totals=ReportTotals(**_totals_fields(totals, cost)),
        periods=[ReportPeriod(start=start_day, **_totals_fields(t, cost)) for start_day, t in reports.by_period(sales, profit_map, granularity)],
        receivables=[ReceivableRow(booking_reference=r.booking_id, customer=r.customer_name or r.agency_name, status=r.status,
                                   total=money(r.total_minor or 0, r.currency or "PKR"), due=money(due, r.currency or "PKR"),
                                   hold_expires_at=r.hold_expires_at) for r, due in owed[:200]],
        receivables_total=money(sum(due for r, due in owed if (r.currency or "PKR") == "PKR"), "PKR"),
        refunds_due=money(sum(r["refundable_minor"] for r in due_rows) + sum(r.amount_minor for r in approved + requested), "PKR"),
        refunds_due_count=len(due_rows) + len(approved) + len(requested),
        payments_by_method=[MethodRow(method=m, account=a, count=n, amount=money(amt, "PKR"), card_fees=money(fee, "PKR"))
                            for m, a, n, amt, fee in reports.payments_by_method(start, end)],
    )
    if cost:
        report.by_booking = [BookingProfitRow(
            booking_reference=r.booking_id, created_at=r.created_at, product=r.product, customer=r.customer_name or r.agency_name,
            sales=money(p.selling_minor, r.currency or "PKR"),
            supplier_charge=None if p.supplier_charge_minor is None else money(p.supplier_charge_minor, r.currency or "PKR"),
            supplier_charge_basis=p.supplier_charge_basis, payment_fees=money(p.payment_fees_minor, r.currency or "PKR"),
            refunds=money(p.refunds_minor, r.currency or "PKR"), profit=None if p.profit_minor is None else money(p.profit_minor, r.currency or "PKR"))
            for r, p in sorted(((r, profit_map[r.booking_id]) for r in sales), key=lambda item: -item[1].selling_minor)[:500]]
        report.supplier_spend = [SupplierSpendRow(day=d, supplier=wallet_store.SUPPLIERS.get(p, (p, ""))[0] if names else None,
                                                  ticket_charges=money(c, "PKR"), top_ups=money(t, "PKR"))
                                 for d, p, c, t in reports.supplier_spend(start, end)]
    return report


@router.get("/reports", response_model=FinanceReport)
async def finance_report(viewer: Annotated[BackOfficeUser, Depends(require_finance)], date_from: date | None = None, date_to: date | None = None,
                         granularity: str = Query("day", pattern=GRANULARITY), product: str | None = Query(None, pattern=PRODUCT)) -> FinanceReport:
    """Daily (weekly, monthly) sales and profit, receivables, refunds due, supplier spend, payments by method / account.
    Same sale rule and profit formula as the dashboard and the booking. Profit and supplier figures need cost and margin."""
    return await run_in_threadpool(build_report, viewer, date_from, date_to, granularity, product)


SECTIONS = ("summary", "profit_by_booking", "receivables", "supplier_spend", "payments_by_method")


def report_sheets(report: FinanceReport) -> dict[str, list[list]]:
    r = xlsx.rupees
    sheets: dict[str, list[list]] = {"summary": [["Period start", "Bookings", "Sales (Rs)", "Fees charged (Rs)", "Supplier charge (Rs)", "Payment fees (Rs)",
                                                   "Refunds (Rs)", "Profit (Rs)", "Profit not known (bookings)"]]}
    for p in report.periods:
        sheets["summary"].append([p.start.isoformat(), p.bookings, r(p.sales.amount_minor), r(p.fees_charged.amount_minor),
                                  r(p.supplier_charge.amount_minor) if p.supplier_charge else None, r(p.payment_fees.amount_minor),
                                  r(p.refunds.amount_minor), r(p.profit.amount_minor) if p.profit else None, p.profit_not_known])
    t = report.totals
    sheets["summary"].append(["Total", t.bookings, r(t.sales.amount_minor), r(t.fees_charged.amount_minor),
                              r(t.supplier_charge.amount_minor) if t.supplier_charge else None, r(t.payment_fees.amount_minor),
                              r(t.refunds.amount_minor), r(t.profit.amount_minor) if t.profit else None, t.profit_not_known])
    if report.by_booking is not None:
        sheets["profit_by_booking"] = [["Booking", "Made on", "Product", "Customer", "Sales (Rs)", "Supplier charge (Rs)", "Basis", "Payment fees (Rs)",
                                        "Refunds (Rs)", "Profit (Rs)"]] + [
            [b.booking_reference, b.created_at.astimezone(PKT).strftime("%Y-%m-%d"), b.product, b.customer or "", r(b.sales.amount_minor),
             r(b.supplier_charge.amount_minor) if b.supplier_charge else "Not known", b.supplier_charge_basis, r(b.payment_fees.amount_minor),
             r(b.refunds.amount_minor), r(b.profit.amount_minor) if b.profit else "Not known"] for b in report.by_booking]
    sheets["receivables"] = [["Booking", "Customer", "Status", "Total (Rs)", "Due (Rs)"]] + [
        [x.booking_reference, x.customer or "", x.status, r(x.total.amount_minor), r(x.due.amount_minor)] for x in report.receivables]
    if report.supplier_spend is not None:
        sheets["supplier_spend"] = [["Day", "Supplier", "Ticket charges (Rs)", "Top-ups (Rs)"]] + [
            [x.day.isoformat(), x.supplier or "", r(x.ticket_charges.amount_minor), r(x.top_ups.amount_minor)] for x in report.supplier_spend]
    sheets["payments_by_method"] = [["Method", "Account", "Payments", "Amount (Rs)", "Card fees (Rs)"]] + [
        [METHOD_LABEL.get(x.method, x.method), x.account or "", x.count, r(x.amount.amount_minor), r(x.card_fees.amount_minor)]
        for x in report.payments_by_method]
    return sheets


def _export_audit(viewer: BackOfficeUser, kind: str, **after) -> None:
    with store.agencies.application_engine().begin() as connection:
        store.audit(connection, actor(viewer), "report_exported", "export", None, after={"format": kind, **after})


@router.get("/reports.xlsx", response_class=Response, responses={200: {"content": {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}}}})
async def export_report_xlsx(viewer: Annotated[BackOfficeUser, Depends(require_finance)], date_from: date | None = None,
                             date_to: date | None = None, granularity: str = Query("day", pattern=GRANULARITY),
                             product: str | None = Query(None, pattern=PRODUCT)) -> Response:
    """Every report section as an Excel sheet (same numbers as /reports). Audited."""
    report = await run_in_threadpool(build_report, viewer, date_from, date_to, granularity, product)
    body = xlsx.workbook({name.replace("_", " ").capitalize(): rows for name, rows in report_sheets(report).items()})
    await run_in_threadpool(_export_audit, viewer, "xlsx", date_from=date_from, date_to=date_to)
    return Response(body, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="travelian-report-{date_from or "all"}-{date_to or "all"}.xlsx"'})


@router.get("/reports.csv", response_class=Response, responses={200: {"content": {"text/csv": {}}}})
async def export_report_csv(viewer: Annotated[BackOfficeUser, Depends(require_finance)], section: str = Query("summary", pattern="^(" + "|".join(SECTIONS) + ")$"),
                            date_from: date | None = None, date_to: date | None = None, granularity: str = Query("day", pattern=GRANULARITY),
                            product: str | None = Query(None, pattern=PRODUCT)) -> Response:
    """One report section as CSV. Audited."""
    report = await run_in_threadpool(build_report, viewer, date_from, date_to, granularity, product)
    sheets = report_sheets(report)
    if section not in sheets:
        raise HTTPException(403, "This report section needs the cost and margin permission.")
    header, *rows = sheets[section]
    body = csv_text(header, [[Number(str(v)) if isinstance(v, (int, Decimal)) and not isinstance(v, bool) else v for v in row] for row in rows])
    await run_in_threadpool(_export_audit, viewer, "csv", section=section, date_from=date_from, date_to=date_to)
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="travelian-{section}-{date_from or "all"}-{date_to or "all"}.csv"'})


# ------------------------------------------------------------------------------------------------ Reconciliation (#71)
from fastapi.responses import JSONResponse  # noqa: E402

from app.db import reconciliation as recon  # noqa: E402
from app.schemas.finance import (  # noqa: E402
    MismatchList, MismatchView, ResolveMismatchRequest, StatementImportList, StatementImportRequest, StatementImportResult, StatementImportView,
    StatementLineList, StatementLineView, StatementPreview, StatementPreviewRequest,
)
from app.services import statement_csv  # noqa: E402



def _recon_error(error: recon.ReconRefused | statement_csv.StatementError) -> JSONResponse:
    if isinstance(error, statement_csv.StatementError):
        return JSONResponse({"detail": str(error), "code": "invalid_file"}, status_code=422)
    return JSONResponse({"detail": error.message, "problems": error.problems[:50]}, status_code=error.status)


@router.post("/reconciliation/preview", response_model=StatementPreview, responses={422: {"description": "Not a readable CSV."}})
async def preview_statement(body: StatementPreviewRequest, viewer: Annotated[BackOfficeUser, Depends(require_finance)]):
    """The file's columns and first rows, so staff can say which column is which. Nothing is saved."""
    try:
        header, rows = statement_csv.read(body.content.encode("utf-8"))
    except statement_csv.StatementError as error:
        return _recon_error(error)
    return StatementPreview(columns=header, sample=rows[:5], rows=len(rows), date_formats=list(statement_csv.DATE_FORMATS))


@router.post("/reconciliation/imports", response_model=StatementImportResult, status_code=201,
             responses={409: {"description": "Already imported."}, 422: {"description": "Rows that can't be read (listed); nothing saved."}})
async def import_statement(body: StatementImportRequest, viewer: Annotated[BackOfficeUser, Depends(require_finance)]):
    """Import a PayFast settlement, a bank statement or a supplier statement and match it. If any row can't be read,
    nothing is saved and the rows are listed. Audited. Changes no payment or booking."""
    if body.kind == "supplier" and not viewer.visibility.cost_and_margin:
        raise HTTPException(403, "Supplier statements need the cost and margin permission.")
    content = body.content.encode("utf-8")
    mapping = {k: v for k, v in body.columns.model_dump().items() if v}
    try:
        header, rows = statement_csv.read(content)
        lines, problems = statement_csv.lines(header, rows, mapping, body.date_format, credits_only=body.kind == "bank")
    except statement_csv.StatementError as error:
        return _recon_error(error)
    if problems:
        return _recon_error(recon.ReconRefused(422, f"{len(problems)} row{'s' if len(problems) != 1 else ''} couldn't be read. Nothing was imported.",
                                               problems))
    if not lines:
        return _recon_error(recon.ReconRefused(422, "No amounts were found with these columns. Nothing was imported."))
    column_map = {**mapping, "date_format": body.date_format}
    try:
        if body.kind == "payfast":
            result = await run_in_threadpool(recon.import_payfast, body.file_name, content, column_map, lines, actor(viewer))
        elif body.kind == "bank":
            if not body.account_id:
                raise recon.ReconRefused(422, "Choose the receiving account this statement belongs to.")
            result = await run_in_threadpool(recon.import_bank, body.account_id, body.file_name, content, column_map, lines, actor(viewer))
        else:
            if not body.provider_id:
                raise recon.ReconRefused(422, "Choose the supplier this statement is from.")
            result = await run_in_threadpool(recon.import_supplier, body.provider_id, body.file_name, content, column_map, lines, actor(viewer))
    except recon.ReconRefused as error:
        return _recon_error(error)
    return StatementImportResult(**result.__dict__)


@router.get("/reconciliation/imports", response_model=StatementImportList)
async def list_statement_imports(viewer: Annotated[BackOfficeUser, Depends(require_finance)],
                                 kind: str | None = Query(None, pattern="^(payfast|bank|supplier)$")) -> StatementImportList:
    rows = await run_in_threadpool(recon.list_imports, kind)
    if not viewer.visibility.cost_and_margin:
        rows = [r for r in rows if r["kind"] != "supplier"]
    if not viewer.visibility.supplier_names:
        rows = [{**r, "provider_id": None} for r in rows]
    return StatementImportList(imports=[StatementImportView(**r) for r in rows])


@router.get("/reconciliation/imports/{import_id}/lines", response_model=StatementLineList)
async def list_statement_lines(import_id: str, viewer: Annotated[BackOfficeUser, Depends(require_finance)],
                               status: str | None = Query(None, pattern="^(matched|suggested|unmatched|ignored)$")) -> StatementLineList:
    try:
        rows = await run_in_threadpool(recon.list_lines, import_id, status, viewer.visibility.cost_and_margin)
    except recon.ReconRefused as error:
        raise HTTPException(error.status, error.message) from None
    pkr = lambda minor: None if minor is None else money(minor, "PKR")  # noqa: E731
    return StatementLineList(lines=[StatementLineView(
        id=r["id"], line_no=r["line_no"], reference=r["reference"], description=r["description"], occurred_on=r["occurred_on"],
        amount=money(r["amount_minor"], "PKR"), fee=pkr(r["fee_minor"]), net=pkr(r["net_minor"]), payout_on=r["payout_on"], status=r["status"],
        booking_reference=r["booking_reference"], payment_reference=r["payment_reference"], payment_amount=pkr(r["payment_amount_minor"])) for r in rows])


@router.post("/reconciliation/lines/{line_id}/confirm", status_code=204, responses={404: {}, 409: {}})
async def confirm_bank_match(line_id: str, viewer: Annotated[BackOfficeUser, Depends(require_finance)]):
    """Staff confirm a suggested bank match. Audited."""
    try:
        await run_in_threadpool(recon.decide_suggestion, line_id, actor(viewer), True)
    except recon.ReconRefused as error:
        return _recon_error(error)
    return Response(status_code=204)


@router.post("/reconciliation/lines/{line_id}/reject", status_code=204, responses={404: {}, 409: {}})
async def reject_bank_match(line_id: str, viewer: Annotated[BackOfficeUser, Depends(require_finance)]):
    """Staff reject a suggested bank match: the credit goes on the mismatch list. Audited."""
    try:
        await run_in_threadpool(recon.decide_suggestion, line_id, actor(viewer), False)
    except recon.ReconRefused as error:
        return _recon_error(error)
    return Response(status_code=204)


@router.post("/reconciliation/supplier-check", response_model=dict)
async def supplier_check(viewer: Annotated[BackOfficeUser, Depends(require_wallet_viewer)]) -> dict:
    """Compare recorded supplier charges with issued tickets and their stored cost; differences go on the mismatch list."""
    return {"mismatches_added": await run_in_threadpool(recon.check_supplier_charges, actor(viewer))}


def _mismatch_view(r: dict) -> MismatchView:
    pkr = lambda minor: None if minor is None else money(minor, "PKR")  # noqa: E731
    return MismatchView(id=r["id"], kind=r["kind"], detail=r["detail"], booking_reference=r["booking_reference"], expected=pkr(r["expected_minor"]),
                        actual=pkr(r["actual_minor"]), status=r["status"], explanation=r["explanation"],
                        explained_by_user_id=r["explained_by_user_id"], explained_at=r["explained_at"], resolution_reason=r["resolution_reason"],
                        resolved_by_user_id=r["resolved_by_user_id"], resolved_at=r["resolved_at"], created_at=r["created_at"])


@router.get("/reconciliation/mismatches", response_model=MismatchList)
async def list_mismatches(viewer: Annotated[BackOfficeUser, Depends(require_finance)],
                          status: str | None = Query("open", pattern="^(open|explained|resolved|all)$"), kind: str | None = Query(None, max_length=40),
                          limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> MismatchList:
    """One list of differences to work through; supplier ones need cost and margin."""
    try:
        rows, total = await run_in_threadpool(lambda: recon.list_mismatches(None if status == "all" else status, kind, limit, offset,
                                                                            viewer.visibility.cost_and_margin))
    except recon.ReconRefused as error:
        raise HTTPException(error.status, error.message) from None
    return MismatchList(mismatches=[_mismatch_view(r) for r in rows], total=total)


@router.post("/reconciliation/mismatches/{mismatch_id}/resolve", response_model=MismatchView, responses={404: {}, 409: {}, 422: {}})
async def resolve_mismatch(mismatch_id: str, body: ResolveMismatchRequest, viewer: Annotated[BackOfficeUser, Depends(require_finance)]):
    """Mark a mismatch explained or resolved, with the reason. Audited; it can't be reopened or edited."""
    try:
        return _mismatch_view(await run_in_threadpool(recon.resolve_mismatch, mismatch_id, actor(viewer), body.status, body.reason,
                                                               viewer.visibility.cost_and_margin))
    except recon.ReconRefused as error:
        return _recon_error(error)
