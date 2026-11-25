"""Finance operations (migration 0037): Ticket Sale Entry, Supplier Refunds, booking supplier settlement, payment shortfall,
sold-through channel, Customer Dues, business/personal accounts, the Finance overview figures. Integer paisa.

Nothing here moves money or calls a supplier: it records what happened, checks it (D52 negative-balance block via
supplier_ledger.covers) and writes a finance_audit_log row in the same transaction. Profit is worked out here, never by
the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies, finance, supplier_ledger as ledger

Refused = finance.FinanceRefused


def _now() -> datetime:
    return datetime.now(timezone.utc)


def rupees(minor: int) -> str:
    return f"Rs {minor / 100:,.0f}" if minor % 100 == 0 else f"Rs {minor / 100:,.2f}"


# ================================================================================================== Ticket Sale Entry
CHANNELS = ("direct", "b2b", "other")
TICKET_STATUSES = ("issued", "on_hold")


@dataclass
class SaleInput:
    channel: str
    agency_id: str | None
    customer_person_id: str | None
    passenger_name: str | None
    travel_date: date | None
    route: str | None
    pnr: str | None
    supplier_id: str | None
    cost_minor: int
    sale_minor: int
    received_minor: int
    payment_account_id: str | None
    ticket_status: str
    notes: str | None


@dataclass
class SalePreview:
    profit_minor: int
    due_minor: int
    effects: list[str]
    problems: list[str]
    supplier_balance_after_minor: int | None = None


def _names(connection, sale: SaleInput) -> tuple[str | None, str | None, str | None, str | None]:
    """(customer name, agency name, supplier name, account label) for the ids given (None when unknown)."""
    customer = connection.scalar(text("SELECT NULLIF(TRIM(CONCAT(first_name, ' ', last_name)), '') FROM saved_people WHERE id = :i"),
                                 {"i": sale.customer_person_id}) if sale.customer_person_id else None
    agency = connection.scalar(text("SELECT name FROM agencies WHERE id = :i"), {"i": sale.agency_id}) if sale.agency_id else None
    supplier = connection.scalar(text("SELECT name FROM suppliers WHERE id = :i AND active"), {"i": sale.supplier_id}) if sale.supplier_id else None
    account = connection.scalar(text("SELECT label FROM payment_accounts WHERE id = :i"), {"i": sale.payment_account_id}) if sale.payment_account_id else None
    return customer, agency, supplier, account


def preview_sale(sale: SaleInput, *, confirmed: bool) -> SalePreview:
    """What saving this entry will do ("This entry will also"), worked out here so the page shows the gateway's numbers."""
    with agencies.application_engine().begin() as connection:
        return _preview(connection, sale, confirmed)


def _preview(connection, sale: SaleInput, confirmed: bool) -> SalePreview:
    customer, agency, supplier, account = _names(connection, sale)
    problems: list[str] = []
    if sale.channel not in CHANNELS:
        problems.append("Choose the channel.")
    if sale.channel == "b2b" and not agency:
        problems.append("Choose the B2B partner.")
    if not customer and not (sale.passenger_name or "").strip():
        problems.append("Find or add the passenger.")
    if sale.supplier_id and not supplier:
        problems.append("Choose the airline ID from the list.")
    if min(sale.cost_minor, sale.sale_minor, sale.received_minor) < 0:
        problems.append("Amounts can't be negative.")
    if sale.received_minor > 0 and not account:
        problems.append("Choose the account the money was received into.")
    if sale.received_minor > sale.sale_minor:
        problems.append("The amount received is more than the sale price.")
    if sale.ticket_status not in TICKET_STATUSES:
        problems.append("Choose the ticket status.")
    profit, due = sale.sale_minor - sale.cost_minor, max(sale.sale_minor - sale.received_minor, 0)
    effects: list[str] = []
    after = None
    who = customer or (sale.passenger_name or "").strip() or "the passenger"
    if not confirmed:
        effects.append("Go to Finance to confirm. Nothing below happens until Finance confirms it.")
    effects.append(f"Record a sale of {rupees(sale.sale_minor)} for {who}" + (f", sold through {agency}" if agency else "") + ".")
    if supplier and sale.ticket_status == "issued":
        s = ledger.supplier(connection, sale.supplier_id)  # type: ignore[arg-type]
        b = ledger.balance(connection, s) if s else None
        if b is not None and b.current_minor is not None:
            after = b.current_minor - sale.cost_minor
            effects.append(f"Deduct {rupees(sale.cost_minor)} from {supplier}'s balance ({rupees(b.current_minor)} → {rupees(after)}).")
            if after < 0:
                problems.append(f"{supplier}'s balance would go below zero ({rupees(after)}). Top up {supplier} first.")
        elif s is not None and s.balance_mode == "api":
            problems.append(f"{supplier}'s balance hasn't been read yet. Refresh it on Supplier Balances first.")
        else:
            effects.append(f"Deduct {rupees(sale.cost_minor)} from {supplier}'s balance (its balance isn't set up yet).")
    elif supplier:
        effects.append(f"No deduction from {supplier} while the ticket is on hold.")
    else:
        effects.append("No airline ID chosen: no supplier balance changes.")
    if sale.received_minor > 0 and account:
        effects.append(f"Add {rupees(sale.received_minor)} to {account} (sales received).")
    if due > 0:
        effects.append(f"Add {rupees(due)} to Customer Dues until it is received.")
    effects.append(f"Profit: {rupees(profit)}." if profit >= 0 else f"Loss: {rupees(-profit)}.")
    return SalePreview(profit, due, effects, problems, after)


@dataclass
class Sale:
    id: str
    number: int
    channel: str
    agency_id: str | None
    agency_name: str | None
    customer_person_id: str | None
    passenger_name: str
    travel_date: date | None
    route: str | None
    pnr: str | None
    supplier_id: str | None
    supplier_name: str | None
    cost_minor: int
    sale_minor: int
    received_minor: int
    payment_account_id: str | None
    payment_account: str | None
    ticket_status: str
    state: str
    notes: str | None
    submitted_by_user_id: str | None
    submitted_by_role: str | None
    submitted_at: datetime
    confirmed_by_user_id: str | None
    confirmed_at: datetime | None
    rejected_reason: str | None

    @property
    def profit_minor(self) -> int:
        return self.sale_minor - self.cost_minor

    @property
    def due_minor(self) -> int:
        return max(self.sale_minor - self.received_minor, 0)


_SALE_SELECT = """SELECT t.*, a.name AS agency_name, s.name AS supplier_name, p.label AS account_label, r.name AS role_name
  FROM ticket_sale_entries t LEFT JOIN agencies a ON a.id = t.agency_id LEFT JOIN suppliers s ON s.id = t.supplier_id
  LEFT JOIN payment_accounts p ON p.id = t.payment_account_id LEFT JOIN users u ON u.id = t.submitted_by_user_id
  LEFT JOIN roles r ON r.id = u.role_id"""


def _sale(row) -> Sale:
    return Sale(row["id"], row["number"], row["channel"], row["agency_id"], row["agency_name"], row["customer_person_id"], row["passenger_name"],
                row["travel_date"], row["route"], row["pnr"], row["supplier_id"], row["supplier_name"], row["cost_minor"], row["sale_minor"],
                row["received_minor"], row["payment_account_id"], row["account_label"], row["ticket_status"], row["state"], row["notes"],
                row["submitted_by_user_id"], row["role_name"], row["submitted_at"], row["confirmed_by_user_id"], row["confirmed_at"],
                row["rejected_reason"])


def create_sale(actor: finance.Actor, sale: SaleInput, *, finance_role: bool) -> Sale:
    """Finance (D55): saved confirmed at once (the supplier deduction, if any, checked against D52 first). Other staff: saved
    pending, for Finance to confirm; nothing is deducted until then."""
    with agencies.application_engine().begin() as connection:
        if sale.supplier_id:
            ledger.lock(connection, sale.supplier_id)
        preview = _preview(connection, sale, finance_role)
        if preview.problems:
            code = ledger.NEGATIVE if any("below zero" in p or "hasn't been read" in p for p in preview.problems) else "invalid_sale"
            raise Refused(409 if code == ledger.NEGATIVE else 422, " ".join(preview.problems), code)
        if finance_role and sale.supplier_id and sale.ticket_status == "issued":
            ledger.covers(connection, sale.supplier_id, sale.cost_minor, what="This sale")
        customer, *_ = _names(connection, sale)
        sale_id = str(uuid4())
        now = _now()
        connection.execute(text(
            "INSERT INTO ticket_sale_entries (id, channel, agency_id, customer_person_id, passenger_name, travel_date, route, pnr, supplier_id,"
            " cost_minor, sale_minor, received_minor, payment_account_id, ticket_status, state, notes, submitted_by_user_id, submitted_at,"
            " confirmed_by_user_id, confirmed_at) VALUES (:id, :ch, :ag, :cp, :pn, :td, :rt, :pnr, :sup, :cost, :sale, :rec, :acc, :ts, :state, :notes,"
            " :u, :now, :cu, :cat)"),
            {"id": sale_id, "ch": sale.channel, "ag": sale.agency_id if sale.channel == "b2b" else None, "cp": sale.customer_person_id,
             "pn": (customer or (sale.passenger_name or "")).strip()[:160], "td": sale.travel_date, "rt": (sale.route or "").strip()[:80] or None,
             "pnr": (sale.pnr or "").strip().upper()[:20] or None, "sup": sale.supplier_id, "cost": sale.cost_minor, "sale": sale.sale_minor,
             "rec": sale.received_minor, "acc": sale.payment_account_id, "ts": sale.ticket_status, "state": "confirmed" if finance_role else "pending",
             "notes": (sale.notes or "").strip() or None, "u": actor.user_id, "now": now,
             "cu": actor.user_id if finance_role else None, "cat": now if finance_role else None})
        finance.audit(connection, actor, "ticket_sale_recorded" if finance_role else "ticket_sale_submitted", "ticket_sale", sale_id,
                      after={"cost_minor": sale.cost_minor, "sale_minor": sale.sale_minor, "received_minor": sale.received_minor,
                             "supplier_id": sale.supplier_id, "ticket_status": sale.ticket_status})
        return _sale(connection.execute(text(_SALE_SELECT + " WHERE t.id = :id"), {"id": sale_id}).mappings().one())


def confirm_sale(actor: finance.Actor, sale_id: str) -> Sale:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("SELECT * FROM ticket_sale_entries WHERE id = :id FOR UPDATE"), {"id": sale_id}).mappings().one_or_none()
        if row is None:
            raise Refused(404, "Ticket sale not found.", "not_found")
        if row["state"] != "pending":
            raise Refused(409, "Only a sale waiting for Finance can be confirmed.", "not_pending")
        if row["supplier_id"] and row["ticket_status"] == "issued":
            ledger.lock(connection, row["supplier_id"])
            ledger.covers(connection, row["supplier_id"], row["cost_minor"], what="Confirming this sale")
        connection.execute(text("UPDATE ticket_sale_entries SET state = 'confirmed', confirmed_by_user_id = :u, confirmed_at = :n WHERE id = :id"),
                           {"u": actor.user_id, "n": _now(), "id": sale_id})
        finance.audit(connection, actor, "ticket_sale_confirmed", "ticket_sale", sale_id, before={"state": "pending"}, after={"state": "confirmed"})
        return _sale(connection.execute(text(_SALE_SELECT + " WHERE t.id = :id"), {"id": sale_id}).mappings().one())


def reject_sale(actor: finance.Actor, sale_id: str, reason: str) -> Sale:
    if not (reason or "").strip():
        raise Refused(422, "Say why the sale is rejected.", "reason_required")
    with agencies.application_engine().begin() as connection:
        state = connection.scalar(text("SELECT state FROM ticket_sale_entries WHERE id = :id FOR UPDATE"), {"id": sale_id})
        if state is None:
            raise Refused(404, "Ticket sale not found.", "not_found")
        if state != "pending":
            raise Refused(409, "Only a sale waiting for Finance can be rejected.", "not_pending")
        connection.execute(text("UPDATE ticket_sale_entries SET state = 'rejected', rejected_reason = :r WHERE id = :id"),
                           {"r": reason.strip(), "id": sale_id})
        finance.audit(connection, actor, "ticket_sale_rejected", "ticket_sale", sale_id, before={"state": "pending"}, after={"state": "rejected"},
                      reason=reason.strip())
        return _sale(connection.execute(text(_SALE_SELECT + " WHERE t.id = :id"), {"id": sale_id}).mappings().one())


def list_sales(*, state: str | None = None, limit: int = 100) -> list[Sale]:
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text(_SALE_SELECT + (" WHERE t.state = :s" if state else "") + " ORDER BY t.submitted_at DESC LIMIT :l"),
                                  {"s": state, "l": limit}).mappings().all()
    return [_sale(r) for r in rows]


# =================================================================================================== Supplier Refunds
REFUND_FLOW = {"requested": ("chased", "received", "written_off"), "chased": ("received", "written_off"), "received": (), "written_off": ()}
REFUND_OPEN = ("requested", "chased")


@dataclass
class SupplierRefund:
    id: str
    number: int
    supplier_id: str
    supplier_name: str
    booking_reference: str | None
    reference: str | None
    amount_minor: int
    applied_minor: int
    reason: str
    status: str
    opened_on: date
    status_changed_at: datetime
    status_note: str | None

    @property
    def remaining_minor(self) -> int:
        return self.amount_minor - self.applied_minor

    def days_outstanding(self, today: date) -> int:
        return (today - self.opened_on).days


_REFUND_SELECT = """SELECT r.*, s.name AS supplier_name, b.reference AS booking_reference,
  COALESCE((SELECT SUM(a.amount_minor) FROM supplier_refund_applications a WHERE a.refund_id = r.id), 0)::bigint AS applied
  FROM supplier_refunds r JOIN suppliers s ON s.id = r.supplier_id LEFT JOIN bookings b ON b.id = r.booking_id"""


def _refund(row) -> SupplierRefund:
    return SupplierRefund(row["id"], row["number"], row["supplier_id"], row["supplier_name"], row["booking_reference"], row["reference"],
                          row["amount_minor"], row["applied"], row["reason"], row["status"], row["opened_on"], row["status_changed_at"],
                          row["status_note"])


def open_refund(actor: finance.Actor, *, supplier_id: str, booking_reference: str | None, reference: str | None, amount_minor: int,
                reason: str, opened_on: date | None) -> SupplierRefund:
    if amount_minor <= 0:
        raise Refused(422, "Enter the amount the supplier owes back.", "invalid_amount")
    if not (reason or "").strip():
        raise Refused(422, "Say why the supplier owes this refund.", "reason_required")
    with agencies.application_engine().begin() as connection:
        if ledger.supplier(connection, supplier_id) is None:
            raise Refused(422, "Choose the supplier.", "unknown_supplier")
        booking_id = None
        if (booking_reference or "").strip():
            booking_id = connection.scalar(text("SELECT id FROM bookings WHERE reference = :r"), {"r": booking_reference.strip().upper()})
            if booking_id is None:
                raise Refused(422, f"No booking {booking_reference.strip().upper()}.", "unknown_booking")
        refund_id = str(uuid4())
        connection.execute(text(
            "INSERT INTO supplier_refunds (id, supplier_id, booking_id, reference, amount_minor, reason, opened_on, created_by_user_id)"
            " VALUES (:id, :s, :b, :ref, :a, :r, COALESCE(:o, CURRENT_DATE), :u)"),
            {"id": refund_id, "s": supplier_id, "b": booking_id, "ref": (reference or "").strip()[:64] or None, "a": amount_minor,
             "r": reason.strip(), "o": opened_on, "u": actor.user_id})
        finance.audit(connection, actor, "supplier_refund_opened", "supplier_refund", refund_id, booking_id=booking_id,
                      after={"supplier_id": supplier_id, "amount_minor": amount_minor}, reason=reason.strip())
        return _refund(connection.execute(text(_REFUND_SELECT + " WHERE r.id = :id"), {"id": refund_id}).mappings().one())


def move_refund(actor: finance.Actor, refund_id: str, status: str, note: str | None) -> SupplierRefund:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(_REFUND_SELECT + " WHERE r.id = :id FOR UPDATE OF r"), {"id": refund_id}).mappings().one_or_none()
        if row is None:
            raise Refused(404, "Supplier refund not found.", "not_found")
        if status not in REFUND_FLOW.get(row["status"], ()):
            raise Refused(409, f"A {row['status'].replace('_', ' ')} refund can't move to {status.replace('_', ' ')}.", "invalid_status")
        if status == "written_off" and not (note or "").strip():
            raise Refused(422, "Say why it is written off.", "reason_required")
        connection.execute(text("UPDATE supplier_refunds SET status = :s, status_note = :n, status_changed_at = :t WHERE id = :id"),
                           {"s": status, "n": (note or "").strip() or None, "t": _now(), "id": refund_id})
        finance.audit(connection, actor, "supplier_refund_status", "supplier_refund", refund_id, before={"status": row["status"]},
                      after={"status": status}, reason=(note or "").strip() or None)
        return _refund(connection.execute(text(_REFUND_SELECT + " WHERE r.id = :id"), {"id": refund_id}).mappings().one())


def list_refunds(*, status: str | None = None, supplier_id: str | None = None) -> list[SupplierRefund]:
    where, params = [], {}
    if status:
        where.append("r.status = :st")
        params["st"] = status
    if supplier_id:
        where.append("r.supplier_id = :sup")
        params["sup"] = supplier_id
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text(_REFUND_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.opened_on, r.number"),
                                  params).mappings().all()
    return [_refund(r) for r in rows]


# ======================================================================== Booking: supplier billable amount and settlement
@dataclass
class Billing:
    billable_minor: int | None
    supplier_id: str | None
    supplier_name: str | None
    settlement_method: str | None
    refund_number: int | None
    credit_used_minor: int
    settled_at: datetime | None
    note: str | None


def billing(booking_id: str, connection=None) -> Billing | None:
    sql = text("SELECT g.*, s.name AS supplier_name, r.number AS refund_number FROM booking_supplier_billing g LEFT JOIN suppliers s ON s.id = g.supplier_id"
               " LEFT JOIN supplier_refunds r ON r.id = g.refund_id WHERE g.booking_id = :b")

    def read(conn):
        row = conn.execute(sql, {"b": booking_id}).mappings().one_or_none()
        if row is None:
            return None
        return Billing(row["billable_minor"], row["supplier_id"], row["supplier_name"], row["settlement_method"], row["refund_number"],
                       row["credit_used_minor"], row["settled_at"], row["note"])
    if connection is not None:
        return read(connection)
    with agencies.application_engine().begin() as conn:
        return read(conn)


def _booking(connection, reference: str):
    row = connection.execute(text("SELECT id, reference, provider_id, total_minor, currency, supplier_cost_minor FROM bookings WHERE reference = :r"
                                  " FOR UPDATE"), {"r": reference.strip().upper()}).mappings().one_or_none()
    if row is None:
        raise Refused(404, "Booking not found.", "not_found")
    return row


def set_billable(actor: finance.Actor, reference: str, *, billable_minor: int, supplier_id: str | None, note: str | None) -> Billing:
    """The supplier billable amount (issue #202): what the supplier actually bills, independent of the hold-time fare. Editable
    until settled; the Fare and Payments tabs read it."""
    if billable_minor <= 0:
        raise Refused(422, "Enter the amount the supplier bills.", "invalid_amount")
    with agencies.application_engine().begin() as connection:
        booking = _booking(connection, reference)
        current = billing(booking["id"], connection)
        if current and current.settled_at:
            raise Refused(409, "The supplier payment is already settled. Record a correction on the Fare tab instead.", "already_settled")
        supplier_id = supplier_id or (current.supplier_id if current else None) or _default_supplier(connection, booking["provider_id"])
        if supplier_id and ledger.supplier(connection, supplier_id) is None:
            raise Refused(422, "Choose the supplier from the list.", "unknown_supplier")
        connection.execute(text(
            "INSERT INTO booking_supplier_billing (booking_id, billable_minor, supplier_id, note, updated_by_user_id, updated_at)"
            " VALUES (:b, :a, :s, :n, :u, clock_timestamp()) ON CONFLICT (booking_id) DO UPDATE SET billable_minor = EXCLUDED.billable_minor,"
            " supplier_id = EXCLUDED.supplier_id, note = EXCLUDED.note, updated_by_user_id = EXCLUDED.updated_by_user_id, updated_at = EXCLUDED.updated_at"),
            {"b": booking["id"], "a": billable_minor, "s": supplier_id, "n": (note or "").strip() or None, "u": actor.user_id})
        finance.audit(connection, actor, "supplier_billable_set", "booking", booking["reference"], booking_id=booking["id"],
                      before={"billable_minor": current.billable_minor if current else None}, after={"billable_minor": billable_minor, "supplier_id": supplier_id},
                      reason=(note or "").strip() or None)
        return billing(booking["id"], connection)  # type: ignore[return-value]


def _default_supplier(connection, provider_id: str | None) -> str | None:
    if not provider_id:
        return None
    return connection.scalar(text("SELECT id FROM suppliers WHERE id = :p"), {"p": provider_id})


def settle(actor: finance.Actor, reference: str, *, method: str, refund_id: str | None) -> Billing:
    """Settle the supplier payment at the billable amount: fresh payment (the supplier's balance pays it) or a refund credit
    (that refund's remaining credit pays what it can; the balance pays the rest). D52: refused when the balance would go
    below zero. The booking's supplier charges are set to the billable amount (append-only: a reversal row and a new row),
    so the Fare tab's supplier charge, the profit and the supplier ledger all show the billed figure."""
    if method not in ("fresh", "refund_credit"):
        raise Refused(422, "Choose how the supplier was paid.", "invalid_method")
    with agencies.application_engine().begin() as connection:
        booking = _booking(connection, reference)
        current = billing(booking["id"], connection)
        if current is None or not current.billable_minor:
            raise Refused(422, "Enter the supplier billable amount first.", "billable_required")
        if current.settled_at:
            raise Refused(409, "The supplier payment is already settled.", "already_settled")
        if not current.supplier_id:
            raise Refused(422, "Choose which supplier billed this booking.", "supplier_required")
        ledger.lock(connection, current.supplier_id)
        credit, refund = 0, None
        if method == "refund_credit":
            if not refund_id:
                raise Refused(422, "Choose the refund credit to use.", "refund_required")
            refund = connection.execute(text(_REFUND_SELECT + " WHERE r.id = :id FOR UPDATE OF r"), {"id": refund_id}).mappings().one_or_none()
            if refund is None:
                raise Refused(404, "Supplier refund not found.", "unknown_refund")
            if refund["supplier_id"] != current.supplier_id:
                raise Refused(422, "That refund credit is with another supplier.", "refund_other_supplier")
            if refund["status"] not in REFUND_OPEN:
                raise Refused(409, "That refund is no longer held as credit by the supplier.", "refund_closed")
            credit = min(refund["amount_minor"] - refund["applied"], current.billable_minor)
            if credit <= 0:
                raise Refused(409, "That refund credit is used up.", "refund_used_up")
        ledger.covers(connection, current.supplier_id, current.billable_minor, credit_minor=credit, what="Settling this booking")
        now = _now()
        if credit:
            connection.execute(text("INSERT INTO supplier_refund_applications (id, refund_id, booking_id, amount_minor, applied_by_user_id, applied_at)"
                                    " VALUES (:id, :r, :b, :a, :u, :t)"),
                               {"id": str(uuid4()), "r": refund_id, "b": booking["id"], "a": credit, "u": actor.user_id, "t": now})
            if refund["amount_minor"] - refund["applied"] - credit == 0:
                # Used up as credit: the supplier has paid it back in kind.
                connection.execute(text("UPDATE supplier_refunds SET status = 'received', status_note = :n, status_changed_at = :t WHERE id = :id"),
                                   {"n": f"Applied in full as credit (booking {booking['reference']}).", "t": now, "id": refund_id})
        # Charges: reverse what is recorded per supplier, then the billable amount for the supplier that billed.
        for provider, total in connection.execute(text("SELECT provider_id, SUM(amount_minor)::bigint FROM supplier_charges WHERE booking_id = :b"
                                                       " GROUP BY provider_id HAVING SUM(amount_minor) <> 0"), {"b": booking["id"]}).all():
            connection.execute(text("INSERT INTO supplier_charges (id, booking_id, provider_id, amount_minor, currency, source, reason, recorded_by_user_id,"
                                    " recorded_at) VALUES (:id, :b, :p, :a, :c, 'manual', :r, :u, :t)"),
                               {"id": str(uuid4()), "b": booking["id"], "p": provider, "a": -total, "c": booking["currency"],
                                "r": f"{ledger.SETTLEMENT_REASON}: replaced by the supplier billable amount", "u": actor.user_id, "t": now})
        connection.execute(text("INSERT INTO supplier_charges (id, booking_id, provider_id, amount_minor, currency, source, reason, recorded_by_user_id, recorded_at)"
                                " VALUES (:id, :b, :p, :a, :c, 'manual', :r, :u, :t)"),
                           {"id": str(uuid4()), "b": booking["id"], "p": current.supplier_id, "a": current.billable_minor, "c": booking["currency"],
                            "r": f"{ledger.SETTLEMENT_REASON} ({'refund credit' if credit else 'fresh payment'})", "u": actor.user_id, "t": now})
        connection.execute(text("UPDATE booking_supplier_billing SET settlement_method = :m, refund_id = :r, credit_used_minor = :c, settled_at = :t,"
                                " settled_by_user_id = :u WHERE booking_id = :b"),
                           {"m": method, "r": refund_id if credit else None, "c": credit, "t": now, "u": actor.user_id, "b": booking["id"]})
        finance.audit(connection, actor, "supplier_payment_settled", "booking", booking["reference"], booking_id=booking["id"],
                      after={"billable_minor": current.billable_minor, "method": method, "credit_used_minor": credit, "supplier_id": current.supplier_id})
        return billing(booking["id"], connection)  # type: ignore[return-value]


# ========================================================================================= Booking: payment shortfall
SHORTFALL_KINDS = {"discount": "Discount", "goodwill": "Goodwill", "channel_rate": "Negotiated channel rate"}


@dataclass
class Shortfall:
    amount_minor: int
    reason_kind: str
    note: str
    recorded_by_user_id: str | None
    recorded_at: datetime


def shortfall(booking_id: str, connection=None) -> Shortfall | None:
    sql = text("SELECT amount_minor, reason_kind, note, recorded_by_user_id, recorded_at FROM booking_payment_shortfalls WHERE booking_id = :b")
    if connection is not None:
        row = connection.execute(sql, {"b": booking_id}).one_or_none()
    else:
        with agencies.application_engine().begin() as conn:
            row = conn.execute(sql, {"b": booking_id}).one_or_none()
    return Shortfall(*row) if row else None


def accept_shortfall(actor: finance.Actor, reference: str, *, reason_kind: str, note: str) -> Shortfall:
    """D57: the booking is accepted as paid with less received than its total (discount, goodwill, negotiated channel rate).
    The difference is recorded with the reason, the booking counts as paid (issuance can go ahead), and the true received
    amount and profit stay as they are. Once per booking."""
    if reason_kind not in SHORTFALL_KINDS:
        raise Refused(422, "Choose the reason: discount, goodwill or negotiated channel rate.", "invalid_reason")
    if not (note or "").strip():
        raise Refused(422, "Explain the difference.", "note_required")
    with agencies.application_engine().begin() as connection:
        booking = _booking(connection, reference)
        if shortfall(booking["id"], connection):
            raise Refused(409, "A difference is already accepted on this booking.", "already_accepted")
        paid = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0)::bigint FROM booking_payments WHERE booking_id = :b"), {"b": booking["id"]})
        difference = (booking["total_minor"] or 0) - paid
        if paid <= 0:
            raise Refused(409, "Record the payment received first.", "no_payment")
        if difference <= 0:
            raise Refused(409, "The payment already covers the total.", "nothing_short")
        connection.execute(text("INSERT INTO booking_payment_shortfalls (booking_id, amount_minor, reason_kind, note, recorded_by_user_id)"
                                " VALUES (:b, :a, :k, :n, :u)"),
                           {"b": booking["id"], "a": difference, "k": reason_kind, "n": note.strip(), "u": actor.user_id})
        connection.execute(text("UPDATE bookings SET payment_status = 'paid', updated_at = now(), revision = revision + 1 WHERE id = :b"), {"b": booking["id"]})
        from app.db.supplier_bookings import _event

        _event(connection, booking["id"], actor.user_id, "payment_shortfall_accepted",
               {"amount_minor": difference, "reason": SHORTFALL_KINDS[reason_kind], "note": note.strip(), "received_minor": paid})
        finance.audit(connection, actor, "payment_shortfall_accepted", "booking", booking["reference"], booking_id=booking["id"],
                      after={"amount_minor": difference, "received_minor": paid, "reason_kind": reason_kind}, reason=note.strip())
        return shortfall(booking["id"], connection)  # type: ignore[return-value]


# ============================================================================================ Booking: sold through
def set_sold_through(actor: finance.Actor, reference: str, value: str | None) -> str | None:
    cleaned = " ".join((value or "").split())[:160] or None
    with agencies.application_engine().begin() as connection:
        booking = _booking(connection, reference)
        before = connection.scalar(text("SELECT sold_through FROM bookings WHERE id = :b"), {"b": booking["id"]})
        connection.execute(text("UPDATE bookings SET sold_through = :v, updated_at = now(), revision = revision + 1 WHERE id = :b"),
                           {"v": cleaned, "b": booking["id"]})
        from app.db.supplier_bookings import _event

        _event(connection, booking["id"], actor.user_id, "sold_through_changed", {"before": before, "after": cleaned})
    return cleaned


# ================================================================================================== Customer Dues
@dataclass
class Due:
    kind: str  # booking | ticket_sale
    reference: str
    customer: str | None
    total_minor: int
    received_minor: int
    due_minor: int
    since: datetime
    payment_account: str | None


OPEN_BOOKING_STATUSES = ("held", "pending_payment", "payment_received", "confirmed", "ticket_issued", "paid_not_ticketed")


def customer_dues() -> list[Due]:
    """Bookings and confirmed Ticket Sale Entries where less has been received than is due (less any accepted difference)."""
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text("""
            SELECT b.reference, COALESCE(NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), ''), a.name) AS customer, b.total_minor,
                   COALESCE((SELECT SUM(p.amount_minor) FROM booking_payments p WHERE p.booking_id = b.id), 0)::bigint AS paid,
                   COALESCE((SELECT s.amount_minor FROM booking_payment_shortfalls s WHERE s.booking_id = b.id), 0)::bigint AS accepted,
                   b.created_at,
                   (SELECT p.received_into FROM booking_payments p WHERE p.booking_id = b.id ORDER BY p.received_at DESC LIMIT 1) AS account
            FROM bookings b LEFT JOIN booking_customers c ON c.booking_id = b.id LEFT JOIN agencies a ON a.id = b.agency_id
            WHERE b.booking_status = ANY(:open) AND b.import_source IS NULL AND COALESCE(b.currency, 'PKR') = 'PKR'"""),
            {"open": list(OPEN_BOOKING_STATUSES)}).mappings().all()
        sales = connection.execute(text("""
            SELECT t.number, t.passenger_name, a.name AS agency, t.sale_minor, t.received_minor, t.submitted_at, p.label AS account
            FROM ticket_sale_entries t LEFT JOIN agencies a ON a.id = t.agency_id LEFT JOIN payment_accounts p ON p.id = t.payment_account_id
            WHERE t.state = 'confirmed' AND t.received_minor < t.sale_minor""")).mappings().all()
    dues: list[Due] = []
    for r in rows:
        due = (r["total_minor"] or 0) - r["paid"] - r["accepted"]
        if due > 0:
            dues.append(Due("booking", r["reference"], r["customer"], r["total_minor"] or 0, r["paid"], due, r["created_at"],
                            r["account"] if r["paid"] else None))
    for t in sales:
        dues.append(Due("ticket_sale", f"TSE-{t['number']}", t["agency"] and f"{t['passenger_name']} ({t['agency']})" or t["passenger_name"],
                        t["sale_minor"], t["received_minor"], t["sale_minor"] - t["received_minor"], t["submitted_at"],
                        t["account"] if t["received_minor"] else None))
    return sorted(dues, key=lambda d: d.since)


# ======================================================================================== Accounts (business / personal)
@dataclass
class AccountBalance:
    id: str
    label: str
    method: str
    bank_name: str | None
    account_title: str | None
    account_number: str | None
    iban: str | None
    ownership: str
    active: bool
    opening_minor: int
    booking_receipts_minor: int
    ticket_sale_receipts_minor: int
    top_ups_paid_minor: int

    @property
    def sales_received_minor(self) -> int:
        return self.booking_receipts_minor + self.ticket_sale_receipts_minor

    @property
    def current_minor(self) -> int:
        return self.opening_minor + self.sales_received_minor - self.top_ups_paid_minor


def account_balances(connection=None) -> list[AccountBalance]:
    """Sales received = bank-transfer / JazzCash booking payments recorded into the account + Ticket Sale Entry amounts
    received into it; supplier top-ups paid from it are taken out."""
    sql = text("""
        SELECT a.id, a.label, a.method, a.bank_name, a.account_title, a.account_number, a.iban, a.ownership, a.active, a.opening_balance_minor,
          COALESCE((SELECT SUM(p.amount_minor) FROM booking_payments p WHERE p.account_id = a.id), 0)::bigint AS bookings,
          COALESCE((SELECT SUM(t.received_minor) FROM ticket_sale_entries t WHERE t.payment_account_id = a.id AND t.state = 'confirmed'), 0)::bigint AS sales,
          COALESCE((SELECT SUM(e.amount_minor) FROM supplier_wallet_entries e WHERE e.paid_from_account_id = a.id AND e.kind = 'top_up'), 0)::bigint AS top_ups
        FROM payment_accounts a ORDER BY a.ownership, a.active DESC, a.label""")

    def read(conn):
        return [AccountBalance(r["id"], r["label"], r["method"], r["bank_name"], r["account_title"], r["account_number"], r["iban"], r["ownership"],
                               r["active"], r["opening_balance_minor"], r["bookings"], r["sales"], r["top_ups"]) for r in conn.execute(sql).mappings().all()]
    if connection is not None:
        return read(connection)
    with agencies.application_engine().begin() as conn:
        return read(conn)


def update_account(actor: finance.Actor, account_id: str, *, ownership: str | None, opening_balance_minor: int | None) -> None:
    with agencies.application_engine().begin() as connection:
        before = connection.execute(text("SELECT ownership, opening_balance_minor FROM payment_accounts WHERE id = :a FOR UPDATE"),
                                    {"a": account_id}).mappings().one_or_none()
        if before is None:
            raise Refused(404, "Account not found.", "not_found")
        if ownership is not None and ownership not in ("business", "personal"):
            raise Refused(422, "Choose business or personal.", "invalid_ownership")
        changes = {k: v for k, v in (("ownership", ownership), ("opening_balance_minor", opening_balance_minor)) if v is not None}
        for key, value in changes.items():
            connection.execute(text(f"UPDATE payment_accounts SET {key} = :v WHERE id = :a"), {"v": value, "a": account_id})
        finance.audit(connection, actor, "account_updated", "payment_account", account_id, before=dict(before), after=changes)


# ==================================================================================================== Overview
@dataclass
class Activity:
    at: datetime
    kind: str  # top_up | ticket_sale
    title: str
    amount_minor: int
    supplier_name: str | None
    by_role: str | None
    link: str | None


def activity(*, start: datetime | None, end: datetime | None, supplier_id: str | None, limit: int = 20) -> list[Activity]:
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text("""
            SELECT * FROM (
              SELECT e.recorded_at AS at, 'top_up' AS kind, e.amount_minor AS amount, s.name AS supplier, e.provider_id AS supplier_id,
                     e.recorded_by_user_id AS by_user, e.reference AS ref, NULL::bigint AS number, NULL::text AS state
              FROM supplier_wallet_entries e LEFT JOIN suppliers s ON s.id = e.provider_id WHERE e.kind = 'top_up'
              UNION ALL
              SELECT t.submitted_at, 'ticket_sale', t.sale_minor, s.name, t.supplier_id, t.submitted_by_user_id, t.pnr, t.number, t.state
              FROM ticket_sale_entries t LEFT JOIN suppliers s ON s.id = t.supplier_id WHERE t.state <> 'rejected'
            ) h WHERE (CAST(:start AS timestamptz) IS NULL OR at >= :start) AND (CAST(:end AS timestamptz) IS NULL OR at < :end)
                  AND (CAST(:sup AS text) IS NULL OR supplier_id = :sup)
            ORDER BY at DESC LIMIT :l"""), {"start": start, "end": end, "sup": supplier_id, "l": limit}).mappings().all()
        roles = ledger._roles(connection, {r["by_user"] for r in rows if r["by_user"]})
    out = []
    for r in rows:
        if r["kind"] == "top_up":
            title = f"Top-up to {r['supplier'] or 'a supplier'}" + (f" (ref {r['ref']})" if r["ref"] else "")
            link = "/admin/finance/supplier-balances"
        else:
            title = f"Ticket Sale Entry #{r['number']}" + (" waiting for Finance" if r["state"] == "pending" else "") + (f" · {r['supplier']}" if r["supplier"] else "")
            link = "/admin/finance/ticket-sales"
        out.append(Activity(r["at"], r["kind"], title, r["amount"], r["supplier"], roles.get(r["by_user"]) if r["by_user"] else None, link))
    return out


def sales_profit(start: datetime | None, end: datetime | None) -> tuple[int, int]:
    """(Ticket Sale Entry profit, count) of confirmed entries submitted in the range."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("SELECT COALESCE(SUM(sale_minor - cost_minor), 0)::bigint, COUNT(*) FROM ticket_sale_entries WHERE state = 'confirmed'"
                                      " AND (CAST(:s AS timestamptz) IS NULL OR submitted_at >= :s) AND (CAST(:e AS timestamptz) IS NULL OR submitted_at < :e)"),
                                 {"s": start, "e": end}).one()
    return row[0], row[1]


def pending_sales_count() -> int:
    with agencies.application_engine().begin() as connection:
        return int(connection.scalar(text("SELECT COUNT(*) FROM ticket_sale_entries WHERE state = 'pending'")) or 0)


def today_pkt() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5)).date()




# ============================================================================================ Booking detail extras
@dataclass
class BookingExtras:
    sold_through: str | None
    shortfall: Shortfall | None
    billing: Billing | None
    refund_credits: list[SupplierRefund]
    suppliers: list[tuple[str, str]]
    paid_minor: int


def booking_extras(reference: str) -> BookingExtras | None:
    """What migration 0037 adds to the booking detail. None before it is applied."""
    from app.db import online_attempts

    with agencies.application_engine().begin() as connection:
        if not online_attempts.has_finance_ops(connection):
            return None
        row = connection.execute(text("SELECT id, provider_id, sold_through FROM bookings WHERE reference = :r"), {"r": reference}).mappings().one_or_none()
        if row is None:
            return None
        found = billing(row["id"], connection)
        supplier_id = (found.supplier_id if found else None) or _default_supplier(connection, row["provider_id"])
        credits = [_refund(r) for r in connection.execute(text(_REFUND_SELECT + " WHERE r.supplier_id = :s AND r.status = ANY(:open) ORDER BY r.opened_on"),
                                                          {"s": supplier_id, "open": list(REFUND_OPEN)}).mappings().all()] if supplier_id else []
        paid = int(connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments WHERE booking_id = :b"), {"b": row["id"]}) or 0)
        return BookingExtras(row["sold_through"], shortfall(row["id"], connection), found, [c for c in credits if c.remaining_minor > 0],
                             [(s.id, s.name) for s in ledger.suppliers(connection)], paid)
