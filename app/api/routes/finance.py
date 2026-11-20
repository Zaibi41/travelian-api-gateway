"""Finance (travelian#91): audit log, per-booking money (profit, supplier charges, payment fees, refunds).

Every route needs the Finance permission (app.core.field_visibility.require_finance): Super Admin always, staff when
granted; agency and customer roles get 403. Nothing here moves money or calls a supplier.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
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
    )


def profit_view(data: store.BookingFinance) -> ProfitView:
    currency = data.booking.currency
    p = calculate(profit_inputs(data))
    return ProfitView(selling=money(p.selling_minor, currency), fees_charged=money(p.fees_charged_minor, currency),
                      supplier_charge=None if p.supplier_charge_minor is None else money(p.supplier_charge_minor, currency),
                      supplier_charge_basis=p.supplier_charge_basis, payment_fees=money(p.payment_fees_minor, currency),
                      refunds=money(p.refunds_minor, currency), profit=None if p.profit_minor is None else money(p.profit_minor, currency),
                      missing=list(p.missing))


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


METHOD_LABEL = {"bank_transfer": "Bank transfer", "jazzcash": "JazzCash", "1link": "1Link", "payfast": "Card (PayFast)"}
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
