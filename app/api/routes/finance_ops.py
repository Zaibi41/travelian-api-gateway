"""Finance operations (migration 0037): Overview, Supplier Balances and Ledger, Ticket Sale Entry, Supplier Refunds,
Customer Dues, business/personal accounts, and the booking detail's supplier settlement, payment difference and sold-through.

Who may do what (the gateway decides; the page only hides what it is told it can't do):
- Finance screens: the Finance permission (require_finance). Supplier figures also need the cost and margin permission.
- Ticket Sale Entry (D55): Finance saves an entry confirmed; any other back-office user submits it for Finance to confirm.
- Accounts: FINANCE_ACCOUNT_ROLES (as Accounts & Ledgers).
- Booking supplier billable amount and settlement: cost and margin, plus Finance or record payments. Payment difference and
  sold-through: Finance or record payments.
Nothing here moves money or calls a supplier.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import FINANCE, RECORD_PAYMENTS, BackOfficeUser, require_back_office, require_finance, require_finance_accounts
from app.db import finance as store, finance_ops as ops, supplier_ledger as ledger
from app.schemas.booking_common import BookingMoney

PKT_OFFSET = timedelta(hours=5)
router = APIRouter(prefix="/admin/finance", tags=["admin finance"], responses={
    403: {"description": "Not allowed for this role."}, 503: {"description": "Database not available."}})
booking_router = APIRouter(prefix="/admin/bookings", tags=["admin bookings"], responses={403: {}, 503: {}})

SUPPLIER_ID = "^[a-z0-9][a-z0-9_-]{1,39}$"
REFERENCE = "^[A-Za-z0-9-]{4,32}$"


def money(amount: int | None) -> BookingMoney | None:
    return None if amount is None else BookingMoney(amount_minor=amount, currency="PKR")


def actor(viewer: BackOfficeUser) -> store.Actor:
    return store.Actor(viewer.user_id, viewer.role)


def refused(error: store.FinanceRefused) -> HTTPException:
    return HTTPException(error.status, error.message, headers={"X-Error-Code": error.code})


async def _call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(lambda: fn(*args, **kwargs))
    except store.FinanceRefused as error:
        raise refused(error) from None


def _is_finance(viewer: BackOfficeUser) -> bool:
    return viewer.can(FINANCE)


async def require_supplier_viewer(viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> BackOfficeUser:
    if not viewer.visibility.cost_and_margin:
        raise HTTPException(403, "Supplier balances need the cost and margin permission.")
    return viewer


def _range(date_from: date | None, date_to: date | None) -> tuple[datetime | None, datetime | None]:
    """Pakistan days to UTC instants: [from 00:00 PKT, to+1 00:00 PKT)."""
    start = datetime.combine(date_from, time.min) - PKT_OFFSET if date_from else None
    end = datetime.combine(date_to + timedelta(days=1), time.min) - PKT_OFFSET if date_to else None
    from datetime import timezone

    return (start.replace(tzinfo=timezone.utc) if start else None, end.replace(tzinfo=timezone.utc) if end else None)


# ============================================================================================ Supplier Balances
class SupplierView(BaseModel):
    id: str
    name: str
    section: Literal["api", "direct"]
    booking_mode: Literal["api", "manual"]
    balance_mode: Literal["api", "manual"]
    settlement_tag: Literal["DTS", "IATA"] | None = Field(None, description="Reference only (D53).")
    opening_balance: BookingMoney | None = None
    opening_set_at: datetime | None = None
    total_topped_up: BookingMoney
    total_deducted: BookingMoney
    current_balance: BookingMoney | None = Field(None, description="None: not set up yet (manual) or not read yet (API).")
    status: Literal["ok", "low", "negative", "not_set_up"]
    low_threshold: BookingMoney | None = None
    last_updated: datetime | None = None
    tracking_since: datetime | None = None


class SupplierList(BaseModel):
    suppliers: list[SupplierView]


def _supplier_view(b: ledger.Balance) -> SupplierView:
    s = b.supplier
    return SupplierView(id=s.id, name=s.name, section=s.section, booking_mode=s.booking_mode, balance_mode=s.balance_mode,  # type: ignore[arg-type]
                        settlement_tag=s.settlement_tag, opening_balance=money(s.opening_balance_minor) if s.opening_set_at else None,  # type: ignore[arg-type]
                        opening_set_at=s.opening_set_at, total_topped_up=money(b.topped_up_minor), total_deducted=money(b.deducted_minor),  # type: ignore[arg-type]
                        current_balance=money(b.current_minor), status=b.status, low_threshold=money(s.low_threshold_minor),  # type: ignore[arg-type]
                        last_updated=b.last_updated, tracking_since=b.tracking_since)


@router.get("/suppliers", response_model=SupplierList)
async def list_suppliers(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)]) -> SupplierList:
    """Every supplier: API-integrated first, then direct airline IDs. Balance, totals, status and threshold."""
    return SupplierList(suppliers=[_supplier_view(b) for b in await run_in_threadpool(ledger.balances)])


class AddSupplierRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    section: Literal["api", "direct"] = "direct"
    settlement_tag: Literal["DTS", "IATA"] | None = None
    low_threshold_minor: int | None = Field(None, ge=0, le=100_000_000_000)


@router.post("/suppliers", response_model=SupplierView, status_code=201, responses={409: {}, 422: {}})
async def add_supplier(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)], body: AddSupplierRequest) -> SupplierView:
    """Onboard a supplier (manual booking and balance). Its balance starts once an opening balance or top-up is recorded."""
    s = await _call(ledger.add_supplier, actor(viewer), name=body.name, section=body.section, settlement_tag=body.settlement_tag,
                    low_threshold_minor=body.low_threshold_minor)
    return await _one(s.id)


async def _one(supplier_id: str) -> SupplierView:
    found = next((b for b in await run_in_threadpool(lambda: ledger.balances(include_inactive=True)) if b.supplier.id == supplier_id), None)
    if found is None:
        raise HTTPException(404, "Supplier not found.")
    return _supplier_view(found)


class UpdateSupplierRequest(BaseModel):
    low_threshold_minor: int | None = Field(None, ge=0, le=100_000_000_000)
    settlement_tag: Literal["DTS", "IATA"] | None = None
    fields: list[Literal["low_threshold_minor", "settlement_tag"]] = Field(description="Which fields to change (null clears).")


@router.patch("/suppliers/{supplier_id}", response_model=SupplierView, responses={404: {}})
async def update_supplier(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)], body: UpdateSupplierRequest,
                          supplier_id: str = PathParam(pattern=SUPPLIER_ID)) -> SupplierView:
    """The low-balance threshold and the DTS / IATA reference tag. The balance itself is never edited here."""
    await _call(ledger.update_supplier, actor(viewer), supplier_id, {f: getattr(body, f) for f in body.fields})
    from app.services import supplier_wallets

    if supplier_id in ("southair", "sooperfare"):
        await supplier_wallets.check_low(supplier_id)
    return await _one(supplier_id)


class OpeningRequest(BaseModel):
    amount_minor: int = Field(ge=-100_000_000_000, le=100_000_000_000)
    as_of: datetime


@router.post("/suppliers/{supplier_id}/opening", response_model=SupplierView, responses={404: {}, 409: {}, 422: {}})
async def set_opening(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)], body: OpeningRequest,
                      supplier_id: str = PathParam(pattern=SUPPLIER_ID)) -> SupplierView:
    """Once per manual supplier: the balance it held on `as_of` (from the portal or the Excel sheet). Later: adjustments."""
    await _call(ledger.set_opening, actor(viewer), supplier_id, body.amount_minor, body.as_of)
    return await _one(supplier_id)


class TopUpRequest(BaseModel):
    kind: Literal["top_up", "adjustment"] = "top_up"
    amount_minor: int = Field(ge=-100_000_000_000, le=100_000_000_000)
    reference: str | None = Field(None, max_length=120)
    reason: str | None = Field(None, max_length=500)
    occurred_at: datetime
    paid_from_account_id: str | None = Field(None, max_length=36)


@router.post("/suppliers/{supplier_id}/top-ups", response_model=SupplierView, status_code=201, responses={404: {}, 409: {}, 422: {}})
async def add_top_up(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)], body: TopUpRequest,
                     supplier_id: str = PathParam(pattern=SUPPLIER_ID)) -> SupplierView:
    """A top-up (adds) or an adjustment (+/-, with a reason; refused if it would take the balance below zero, D52)."""
    await _call(ledger.add_top_up, actor(viewer), supplier_id, kind=body.kind, amount_minor=body.amount_minor, reference=body.reference,
                reason=body.reason, occurred_at=body.occurred_at, paid_from_account_id=body.paid_from_account_id)
    from app.services import supplier_wallets

    if supplier_id == "sooperfare":
        await supplier_wallets.check_low(supplier_id)
    return await _one(supplier_id)


class LedgerItemView(BaseModel):
    at: datetime
    kind: Literal["opening", "top_up", "adjustment", "refund_credit", "booking_charge", "ticket_sale", "balance_check"]
    amount: BookingMoney | None = Field(None, description="+ adds, - deducts. None for a balance read.")
    balance_after: BookingMoney | None = Field(None, description="Running balance (manual suppliers), or the balance read (API).")
    description: str
    reference: str | None = None
    booking_reference: str | None = None
    ticket_sale_number: int | None = None
    by_role: str | None = None
    counted: bool = Field(True, description="False: before the balance started (shown for history, not in the balance).")


class LedgerView(BaseModel):
    supplier: SupplierView
    items: list[LedgerItemView] = Field(description="Oldest first.")


@router.get("/suppliers/{supplier_id}/ledger", response_model=LedgerView, responses={404: {}})
async def supplier_ledger(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)],
                          supplier_id: str = PathParam(pattern=SUPPLIER_ID)) -> LedgerView:
    """Every balance-affecting event in order: opening, top-ups, adjustments, refund credits applied, and each deduction with
    what caused it (a Ticket Sale Entry, a booking issued automatically or by staff, or a Supplier payment settlement)."""
    s, b, items = await _call(ledger.ledger, supplier_id)
    return LedgerView(supplier=_supplier_view(b), items=[LedgerItemView(
        at=i.at, kind=i.kind, amount=money(i.amount_minor), balance_after=money(i.balance_minor), description=i.description,  # type: ignore[arg-type]
        reference=i.reference, booking_reference=i.booking_reference, ticket_sale_number=i.ticket_sale_number, by_role=i.by_role,
        counted=i.counted) for i in items])


# ============================================================================================ Ticket Sale Entry
class SaleRequest(BaseModel):
    channel: Literal["direct", "b2b", "other"]
    agency_id: str | None = Field(None, max_length=36)
    customer_person_id: str | None = Field(None, max_length=36)
    passenger_name: str | None = Field(None, max_length=160)
    travel_date: date | None = None
    route: str | None = Field(None, max_length=80)
    pnr: str | None = Field(None, max_length=20)
    supplier_id: str | None = Field(None, max_length=40)
    cost_minor: int = Field(ge=0, le=100_000_000_000)
    sale_minor: int = Field(ge=0, le=100_000_000_000)
    received_minor: int = Field(0, ge=0, le=100_000_000_000)
    payment_account_id: str | None = Field(None, max_length=36)
    ticket_status: Literal["issued", "on_hold"] = "issued"
    notes: str | None = Field(None, max_length=1000)

    def to_input(self) -> ops.SaleInput:
        return ops.SaleInput(**self.model_dump())


class SalePreviewView(BaseModel):
    profit: BookingMoney
    due: BookingMoney
    effects: list[str] = Field(description="What saving will do, in order (the 'This entry will also' box).")
    problems: list[str] = Field(description="Why it can't be saved yet; empty when it can.")
    confirmed_on_save: bool = Field(description="True for Finance (saved confirmed); False: submitted for Finance to confirm.")


@router.post("/ticket-sales/preview", response_model=SalePreviewView)
async def preview_sale(viewer: Annotated[BackOfficeUser, Depends(require_back_office)], body: SaleRequest) -> SalePreviewView:
    """Worked out by the gateway as the form is filled: profit, what's due, and what saving will do. Saves nothing."""
    p = await _call(ops.preview_sale, body.to_input(), confirmed=_is_finance(viewer))
    return SalePreviewView(profit=money(p.profit_minor), due=money(p.due_minor), effects=p.effects, problems=p.problems,  # type: ignore[arg-type]
                           confirmed_on_save=_is_finance(viewer))


class SaleView(BaseModel):
    id: str
    number: int
    channel: Literal["direct", "b2b", "other"]
    agency_name: str | None = None
    passenger_name: str
    travel_date: date | None = None
    route: str | None = None
    pnr: str | None = None
    supplier_id: str | None = None
    supplier_name: str | None = None
    cost: BookingMoney
    sale: BookingMoney
    received: BookingMoney
    due: BookingMoney
    profit: BookingMoney
    payment_account: str | None = None
    ticket_status: Literal["issued", "on_hold"]
    state: Literal["confirmed", "pending", "rejected"]
    notes: str | None = None
    submitted_by_role: str | None = None
    submitted_at: datetime
    confirmed_at: datetime | None = None
    rejected_reason: str | None = None


def _sale_view(s: ops.Sale) -> SaleView:
    return SaleView(id=s.id, number=s.number, channel=s.channel, agency_name=s.agency_name, passenger_name=s.passenger_name,  # type: ignore[arg-type]
                    travel_date=s.travel_date, route=s.route, pnr=s.pnr, supplier_id=s.supplier_id, supplier_name=s.supplier_name,
                    cost=money(s.cost_minor), sale=money(s.sale_minor), received=money(s.received_minor), due=money(s.due_minor),  # type: ignore[arg-type]
                    profit=money(s.profit_minor), payment_account=s.payment_account, ticket_status=s.ticket_status, state=s.state,  # type: ignore[arg-type]
                    notes=s.notes, submitted_by_role=s.submitted_by_role, submitted_at=s.submitted_at, confirmed_at=s.confirmed_at,
                    rejected_reason=s.rejected_reason)


@router.post("/ticket-sales", response_model=SaleView, status_code=201, responses={409: {"description": "negative_balance (D52)."}, 422: {}})
async def create_sale(viewer: Annotated[BackOfficeUser, Depends(require_back_office)], body: SaleRequest) -> SaleView:
    """D55: Finance saves it confirmed (and the supplier deduction, if any, is checked against D52 first); anyone else submits it
    for Finance to confirm."""
    return _sale_view(await _call(ops.create_sale, actor(viewer), body.to_input(), finance_role=_is_finance(viewer)))


class SaleList(BaseModel):
    items: list[SaleView]
    can_confirm: bool


@router.get("/ticket-sales", response_model=SaleList)
async def list_sales(viewer: Annotated[BackOfficeUser, Depends(require_back_office)],
                     state: Literal["confirmed", "pending", "rejected"] | None = None) -> SaleList:
    """Finance sees every entry; other staff see only the ones they submitted."""
    items = await run_in_threadpool(lambda: ops.list_sales(state=state, limit=200))
    if not _is_finance(viewer):
        items = [s for s in items if s.submitted_by_user_id == viewer.user_id]
    return SaleList(items=[_sale_view(s) for s in items], can_confirm=_is_finance(viewer))


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/ticket-sales/{sale_id}/confirm", response_model=SaleView, responses={404: {}, 409: {}})
async def confirm_sale(viewer: Annotated[BackOfficeUser, Depends(require_finance)], sale_id: str = PathParam(max_length=36)) -> SaleView:
    return _sale_view(await _call(ops.confirm_sale, actor(viewer), sale_id))


@router.post("/ticket-sales/{sale_id}/reject", response_model=SaleView, responses={404: {}, 409: {}, 422: {}})
async def reject_sale(viewer: Annotated[BackOfficeUser, Depends(require_finance)], body: RejectRequest,
                      sale_id: str = PathParam(max_length=36)) -> SaleView:
    return _sale_view(await _call(ops.reject_sale, actor(viewer), sale_id, body.reason))


class Option(BaseModel):
    id: str
    name: str
    detail: str | None = None


class SaleOptions(BaseModel):
    suppliers: list[Option]
    accounts: list[Option]
    partners: list[Option] = Field(description="B2B partner agencies.")
    is_finance: bool


@router.get("/ticket-sales/options", response_model=SaleOptions)
async def sale_options(viewer: Annotated[BackOfficeUser, Depends(require_back_office)]) -> SaleOptions:
    from sqlalchemy import text

    def read():
        with ops.agencies.application_engine().begin() as connection:
            suppliers = [Option(id=s.id, name=s.name, detail="API" if s.section == "api" else "Direct airline ID") for s in ledger.suppliers(connection)]
            accounts = [Option(id=r[0], name=r[1], detail=r[2]) for r in connection.execute(text(
                "SELECT id, label, ownership FROM payment_accounts WHERE active ORDER BY ownership, label")).all()]
            partners = [Option(id=r[0], name=r[1]) for r in connection.execute(text("SELECT id, name FROM agencies ORDER BY name LIMIT 500")).all()]
        return SaleOptions(suppliers=suppliers, accounts=accounts, partners=partners, is_finance=_is_finance(viewer))
    return await run_in_threadpool(read)


# ============================================================================================ Supplier Refunds
class SupplierRefundView(BaseModel):
    id: str
    number: int
    supplier_id: str
    supplier_name: str
    booking_reference: str | None = None
    reference: str | None = None
    amount: BookingMoney
    applied: BookingMoney
    remaining: BookingMoney
    reason: str
    status: Literal["requested", "chased", "received", "written_off"]
    opened_on: date
    days_outstanding: int
    status_note: str | None = None
    next_statuses: list[str]
    can_apply_credit: bool


def _refund_view(r: ops.SupplierRefund) -> SupplierRefundView:
    return SupplierRefundView(id=r.id, number=r.number, supplier_id=r.supplier_id, supplier_name=r.supplier_name, booking_reference=r.booking_reference,
                      reference=r.reference, amount=money(r.amount_minor), applied=money(r.applied_minor), remaining=money(r.remaining_minor),  # type: ignore[arg-type]
                      reason=r.reason, status=r.status, opened_on=r.opened_on, days_outstanding=r.days_outstanding(ops.today_pkt()),  # type: ignore[arg-type]
                      status_note=r.status_note, next_statuses=list(ops.REFUND_FLOW.get(r.status, ())),
                      can_apply_credit=r.status in ops.REFUND_OPEN and r.remaining_minor > 0)


class SupplierRefundList(BaseModel):
    items: list[SupplierRefundView]
    pending_count: int
    pending_total: BookingMoney


@router.get("/supplier-refunds", response_model=SupplierRefundList)
async def list_refunds(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)],
                       status: Literal["requested", "chased", "received", "written_off"] | None = None,
                       supplier_id: str | None = Query(None, pattern=SUPPLIER_ID)) -> SupplierRefundList:
    items = await run_in_threadpool(lambda: ops.list_refunds(status=status, supplier_id=supplier_id))
    pending = await run_in_threadpool(lambda: [r for r in ops.list_refunds() if r.status in ops.REFUND_OPEN])
    return SupplierRefundList(items=[_refund_view(r) for r in items], pending_count=len(pending),
                      pending_total=money(sum(r.remaining_minor for r in pending)))  # type: ignore[arg-type]


class OpenRefundRequest(BaseModel):
    supplier_id: str = Field(pattern=SUPPLIER_ID)
    booking_reference: str | None = Field(None, max_length=32)
    reference: str | None = Field(None, max_length=64)
    amount_minor: int = Field(gt=0, le=100_000_000_000)
    reason: str = Field(min_length=1, max_length=1000)
    opened_on: date | None = None


@router.post("/supplier-refunds", response_model=SupplierRefundView, status_code=201, responses={422: {}})
async def open_refund(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)], body: OpenRefundRequest) -> SupplierRefundView:
    return _refund_view(await _call(ops.open_refund, actor(viewer), **body.model_dump()))


class RefundStatusRequest(BaseModel):
    status: Literal["chased", "received", "written_off"]
    note: str | None = Field(None, max_length=500)


@router.post("/supplier-refunds/{refund_id}/status", response_model=SupplierRefundView, responses={404: {}, 409: {}, 422: {}})
async def move_refund(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)], body: RefundStatusRequest,
                      refund_id: str = PathParam(max_length=36)) -> SupplierRefundView:
    return _refund_view(await _call(ops.move_refund, actor(viewer), refund_id, body.status, body.note))


class ApplyCreditRequest(BaseModel):
    booking_reference: str = Field(pattern=REFERENCE)
    billable_minor: int | None = Field(None, gt=0, le=100_000_000_000, description="Set the booking's supplier billable amount first (if not set).")


@router.post("/supplier-refunds/{refund_id}/apply", response_model=SupplierRefundView, responses={404: {}, 409: {}, 422: {}})
async def apply_credit(viewer: Annotated[BackOfficeUser, Depends(require_supplier_viewer)], body: ApplyCreditRequest,
                       refund_id: str = PathParam(max_length=36)) -> SupplierRefundView:
    """D56: settle a booking's supplier charge with this refund credit instead of fresh cash (the balance pays any rest; D52)."""
    refund = next((r for r in await run_in_threadpool(ops.list_refunds) if r.id == refund_id), None)
    if refund is None:
        raise HTTPException(404, "Supplier refund not found.")
    if body.billable_minor is not None:
        await _call(ops.set_billable, actor(viewer), body.booking_reference, billable_minor=body.billable_minor, supplier_id=refund.supplier_id, note=None)
    await _call(ops.settle, actor(viewer), body.booking_reference, method="refund_credit", refund_id=refund_id)
    updated = next(r for r in await run_in_threadpool(ops.list_refunds) if r.id == refund_id)
    return _refund_view(updated)


# ================================================================================================ Customer Dues
class DueView(BaseModel):
    kind: Literal["booking", "ticket_sale"]
    reference: str
    customer: str | None = None
    total: BookingMoney
    received: BookingMoney
    due: BookingMoney
    days_outstanding: int
    payment_account: str | None = Field(None, description="Where the part payment went (partly paid only).")


class DueList(BaseModel):
    items: list[DueView]
    total_due: BookingMoney


@router.get("/customer-dues", response_model=DueList)
async def customer_dues(viewer: Annotated[BackOfficeUser, Depends(require_finance)]) -> DueList:
    dues = await run_in_threadpool(ops.customer_dues)
    today = ops.today_pkt()
    return DueList(items=[DueView(kind=d.kind, reference=d.reference, customer=d.customer, total=money(d.total_minor), received=money(d.received_minor),  # type: ignore[arg-type]
                                  due=money(d.due_minor), days_outstanding=max((today - (d.since + PKT_OFFSET).date()).days, 0),  # type: ignore[arg-type]
                                  payment_account=d.payment_account) for d in dues],
                   total_due=money(sum(d.due_minor for d in dues)))  # type: ignore[arg-type]


# ================================================================================================ Accounts
class AccountView(BaseModel):
    id: str
    label: str
    method: str
    bank_name: str | None = None
    account_title: str | None = None
    account_number: str | None = None
    iban: str | None = None
    ownership: Literal["business", "personal"]
    active: bool
    opening_balance: BookingMoney
    sales_received: BookingMoney = Field(description="Booking payments recorded into it + Ticket Sale Entry amounts received into it.")
    top_ups_paid: BookingMoney = Field(description="Supplier top-ups paid from it.")
    current_balance: BookingMoney


class AccountList(BaseModel):
    business: list[AccountView]
    personal: list[AccountView]
    business_total: BookingMoney
    personal_total: BookingMoney


def _account_view(a: ops.AccountBalance) -> AccountView:
    return AccountView(id=a.id, label=a.label, method=a.method, bank_name=a.bank_name, account_title=a.account_title, account_number=a.account_number,
                       iban=a.iban, ownership=a.ownership, active=a.active, opening_balance=money(a.opening_minor),  # type: ignore[arg-type]
                       sales_received=money(a.sales_received_minor), top_ups_paid=money(a.top_ups_paid_minor), current_balance=money(a.current_minor))  # type: ignore[arg-type]


@router.get("/accounts", response_model=AccountList)
async def accounts(viewer: Annotated[BackOfficeUser, Depends(require_finance_accounts)]) -> AccountList:
    """Business and personal accounts (D54: personal accounts are never in the company totals)."""
    rows = await run_in_threadpool(ops.account_balances)
    business, personal = [a for a in rows if a.ownership == "business"], [a for a in rows if a.ownership == "personal"]
    return AccountList(business=[_account_view(a) for a in business], personal=[_account_view(a) for a in personal],
                       business_total=money(sum(a.current_minor for a in business if a.active)),  # type: ignore[arg-type]
                       personal_total=money(sum(a.current_minor for a in personal if a.active)))  # type: ignore[arg-type]


class AccountUpdate(BaseModel):
    ownership: Literal["business", "personal"] | None = None
    opening_balance_minor: int | None = Field(None, ge=-100_000_000_000, le=100_000_000_000)


@router.patch("/accounts/{account_id}", response_model=AccountList, responses={404: {}, 422: {}})
async def update_account(viewer: Annotated[BackOfficeUser, Depends(require_finance_accounts)], body: AccountUpdate,
                         account_id: str = PathParam(max_length=36)) -> AccountList:
    await _call(ops.update_account, actor(viewer), account_id, ownership=body.ownership, opening_balance_minor=body.opening_balance_minor)
    return await accounts(viewer)


# ================================================================================================ Overview
class SupplierAttention(BaseModel):
    supplier_id: str
    supplier_name: str
    status: Literal["low", "negative"]
    current_balance: BookingMoney
    low_threshold: BookingMoney | None = None


class ActivityView(BaseModel):
    at: datetime
    kind: Literal["top_up", "ticket_sale"]
    title: str
    amount: BookingMoney
    by_role: str | None = None
    link: str | None = None


class OverviewView(BaseModel):
    total_supplier_balance: BookingMoney | None = Field(None, description="Suppliers whose balance is known. Cost and margin only.")
    suppliers_attention_count: int = Field(0, description="Negative or below threshold.")
    suppliers_negative_count: int = 0
    total_bank_balance: BookingMoney
    include_personal: bool
    total_customer_dues: BookingMoney
    customer_dues_count: int
    total_profit: BookingMoney | None = Field(None, description="Booking profit (as Reports) + Ticket Sale Entry profit, in the range. Cost and margin only.")
    profit_not_known: int = 0
    pending_refunds_count: int
    pending_refunds_total: BookingMoney
    pending_sales_count: int
    attention: list[SupplierAttention]
    activity: list[ActivityView]
    suppliers: list[Option]


@router.get("/overview", response_model=OverviewView)
async def overview(viewer: Annotated[BackOfficeUser, Depends(require_finance)], date_from: date | None = None, date_to: date | None = None,
                   account_type: Literal["business", "all"] = "business", supplier_id: str | None = Query(None, pattern=SUPPLIER_ID)) -> OverviewView:
    """Finance overview tiles. Supplier and profit figures need the cost and margin permission."""
    start, end = _range(date_from, date_to)
    cost = viewer.visibility.cost_and_margin

    def build() -> OverviewView:
        balances = [b for b in ledger.balances() if not supplier_id or b.supplier.id == supplier_id]
        known = [b for b in balances if b.current_minor is not None]
        attention = [b for b in known if b.status in ("low", "negative")]
        accounts = [a for a in ops.account_balances() if a.active and (account_type == "all" or a.ownership == "business")]
        dues = ops.customer_dues()
        refunds = [r for r in ops.list_refunds(supplier_id=supplier_id) if r.status in ops.REFUND_OPEN]
        view = OverviewView(
            suppliers_attention_count=len(attention) if cost else 0, suppliers_negative_count=sum(1 for b in attention if b.status == "negative") if cost else 0,
            total_bank_balance=money(sum(a.current_minor for a in accounts)), include_personal=account_type == "all",  # type: ignore[arg-type]
            total_customer_dues=money(sum(d.due_minor for d in dues)), customer_dues_count=len(dues),  # type: ignore[arg-type]
            pending_refunds_count=len(refunds), pending_refunds_total=money(sum(r.remaining_minor for r in refunds)),  # type: ignore[arg-type]
            pending_sales_count=ops.pending_sales_count(),
            attention=[SupplierAttention(supplier_id=b.supplier.id, supplier_name=b.supplier.name, status=b.status, current_balance=money(b.current_minor),  # type: ignore[arg-type]
                                     low_threshold=money(b.supplier.low_threshold_minor)) for b in attention] if cost else [],
            activity=[ActivityView(at=a.at, kind=a.kind, title=a.title, amount=money(a.amount_minor), by_role=a.by_role, link=a.link)  # type: ignore[arg-type]
                      for a in ops.activity(start=start, end=end, supplier_id=supplier_id)] if cost else [],
            suppliers=[Option(id=b.supplier.id, name=b.supplier.name) for b in ledger.balances()] if cost else [],
        )
        if cost:
            view.total_supplier_balance = money(sum(b.current_minor for b in known))  # type: ignore[misc]
            from app.db.bookings import list_dashboard_bookings
            from app.services import finance_reports as reports

            rows = [r for r in list_dashboard_bookings() if r.product not in ("tour", "visa")]
            sales = reports.sales_in(rows, date_from, date_to, None)
            profit_map = reports.profits(sales)
            booking_profit = sum(p.profit_minor for p in profit_map.values() if p.profit_minor is not None)
            view.profit_not_known = sum(1 for p in profit_map.values() if p.profit_minor is None)
            sale_profit, _ = ops.sales_profit(start, end)
            view.total_profit = money(booking_profit + sale_profit)
        return view
    return await run_in_threadpool(build)


# ================================================================================================ Booking detail
def _require_booking_money(viewer: BackOfficeUser, *, cost: bool) -> None:
    if not (viewer.can(FINANCE) or viewer.can(RECORD_PAYMENTS)):
        raise HTTPException(403, "Needs the Finance or record payments permission.")
    if cost and not viewer.visibility.cost_and_margin:
        raise HTTPException(403, "Needs the cost and margin permission.")


class BillableRequest(BaseModel):
    billable_minor: int = Field(gt=0, le=100_000_000_000)
    supplier_id: str | None = Field(None, pattern=SUPPLIER_ID)
    note: str | None = Field(None, max_length=500)


class BillingView(BaseModel):
    billable_amount: BookingMoney
    supplier_id: str | None = None
    supplier_name: str | None = None
    settlement_method: Literal["fresh", "refund_credit"] | None = None
    refund_number: int | None = None
    credit_used: BookingMoney | None = None
    settled_at: datetime | None = None


def _billing_view(b: ops.Billing) -> BillingView:
    return BillingView(billable_amount=money(b.billable_minor), supplier_id=b.supplier_id, supplier_name=b.supplier_name,  # type: ignore[arg-type]
                       settlement_method=b.settlement_method, refund_number=b.refund_number,  # type: ignore[arg-type]
                       credit_used=money(b.credit_used_minor) if b.credit_used_minor else None, settled_at=b.settled_at)


@booking_router.put("/{reference}/supplier-billing", response_model=BillingView, responses={404: {}, 409: {}, 422: {}})
async def set_billable(viewer: Annotated[BackOfficeUser, Depends(require_back_office)], body: BillableRequest,
                       reference: str = PathParam(pattern=REFERENCE)) -> BillingView:
    """Issue #202: the supplier billable amount, the one figure the Fare and Payments tabs read. Editable until settled."""
    _require_booking_money(viewer, cost=True)
    return _billing_view(await _call(ops.set_billable, actor(viewer), reference, billable_minor=body.billable_minor, supplier_id=body.supplier_id,
                                     note=body.note))


class SettleRequest(BaseModel):
    method: Literal["fresh", "refund_credit"]
    refund_id: str | None = Field(None, max_length=36)


@booking_router.post("/{reference}/supplier-billing/settle", response_model=BillingView,
                     responses={404: {}, 409: {"description": "negative_balance (D52), already settled, refund closed."}, 422: {}})
async def settle(viewer: Annotated[BackOfficeUser, Depends(require_back_office)], body: SettleRequest,
                 reference: str = PathParam(pattern=REFERENCE)) -> BillingView:
    """Settle at the billable amount: fresh payment (the supplier balance pays) or a refund credit (D56). D52: no negative balance."""
    _require_booking_money(viewer, cost=True)
    return _billing_view(await _call(ops.settle, actor(viewer), reference, method=body.method, refund_id=body.refund_id))


class ShortfallRequest(BaseModel):
    reason_kind: Literal["discount", "goodwill", "channel_rate"]
    note: str = Field(min_length=1, max_length=500)


class ShortfallView(BaseModel):
    amount: BookingMoney
    reason_kind: Literal["discount", "goodwill", "channel_rate"]
    reason: str
    note: str
    recorded_at: datetime


@booking_router.post("/{reference}/payment-shortfall", response_model=ShortfallView, status_code=201, responses={404: {}, 409: {}, 422: {}})
async def accept_shortfall(viewer: Annotated[BackOfficeUser, Depends(require_back_office)], body: ShortfallRequest,
                           reference: str = PathParam(pattern=REFERENCE)) -> ShortfallView:
    """D57: accept the booking as paid with less received than its total, with the reason. Issuance can then go ahead."""
    _require_booking_money(viewer, cost=False)
    s = await _call(ops.accept_shortfall, actor(viewer), reference, reason_kind=body.reason_kind, note=body.note)
    return ShortfallView(amount=money(s.amount_minor), reason_kind=s.reason_kind, reason=ops.SHORTFALL_KINDS[s.reason_kind], note=s.note,  # type: ignore[arg-type]
                         recorded_at=s.recorded_at)


class SoldThroughRequest(BaseModel):
    sold_through: str | None = Field(None, max_length=160)


class SoldThroughView(BaseModel):
    sold_through: str | None = None


@booking_router.put("/{reference}/sold-through", response_model=SoldThroughView, responses={404: {}})
async def set_sold_through(viewer: Annotated[BackOfficeUser, Depends(require_back_office)], body: SoldThroughRequest,
                           reference: str = PathParam(pattern=REFERENCE)) -> SoldThroughView:
    """The reseller or channel the booking was sold through (not the customer). Empty clears it."""
    _require_booking_money(viewer, cost=False)
    return SoldThroughView(sold_through=await _call(ops.set_sold_through, actor(viewer), reference, body.sold_through))
