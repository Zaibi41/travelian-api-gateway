"""Bookings settled and ticketed outside Travelian (travelian#202, phase 1; migration 0035).

- record_issue: "Mark as issued" for a flight whose supplier can't issue from Travelian (SooperFare today). The staff
  member issued it in the supplier's portal and records the ticket number for every traveler, optionally the ticket
  document, and (cost and margin only) what the supplier actually billed and how it was paid:
    supplier_wallet / credit: the charge draws down the supplier's calculated balance (a refund credit kept with the
                              supplier is part of that balance, recorded in Finance > Supplier balances);
    bank:                     fresh money for this ticket: a top-up of the same amount is recorded with the charge,
                              so the balance doesn't move.
  The supplier charge is recorded once, as the booking's issuance charge (app.db.finance.record_issue_charges uses the
  hold-time cost when no billed amount is given).
- settle_due: the customer paid less than the total by agreement. The rest is waived with a reason: the booking total
  becomes what was paid (so the due is 0 everywhere: payments, Customer Dues, profit), and the original total, the
  waived amount, the reason, who and when are kept on the booking.
Every change locks the booking row, writes a booking event and a finance audit entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies, finance, supplier_wallets
from app.db.issuance import _load
from app.db.supplier_bookings import _event

#: Booking statuses a ticket issued outside Travelian may be recorded from: on hold, awaiting payment, or the hold ran
#: out here after the supplier had already ticketed it.
RECORDABLE_STATUSES = ("held", "pending_payment", "expired", "paid_not_ticketed")
PAID_FROM = ("bank", "supplier_wallet", "credit")


class SettlementRefused(Exception):
    def __init__(self, status: int, message: str, code: str = "refused"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass(frozen=True)
class SupplierSettlement:
    """What the supplier billed and how it was paid (all optional; cost and margin only)."""

    billed_minor: int | None = None
    paid_from: str | None = None
    reference: str | None = None
    paid_at: datetime | None = None


@dataclass(frozen=True)
class Settlement:
    supplier_paid_from: str | None
    supplier_payment_reference: str | None
    supplier_paid_at: datetime | None
    ticket_document_key: str | None
    ticket_document_type: str | None
    waived_minor: int | None
    original_total_minor: int | None
    settled_reason: str | None
    settled_at: datetime | None
    issued_at: datetime | None
    billed_minor: int | None = None  # sum of the booking's supplier charges (cost and margin only, shown by the route)


def read(reference: str) -> Settlement | None:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "SELECT b.supplier_paid_from, b.supplier_payment_reference, b.supplier_paid_at, b.ticket_document_key, b.ticket_document_type,"
            " b.settled_waived_minor AS waived_minor, b.settled_original_total_minor AS original_total_minor, b.settled_reason, b.settled_at,"
            " (SELECT max(e.created_at) FROM booking_events e WHERE e.booking_id = b.id AND e.action IN"
            "   ('issuance_issued', 'issuance_found_issued', 'issuance_verified', 'issuance_resolved', 'issuance_recorded_offline')) AS issued_at,"
            " (SELECT SUM(s.amount_minor) FROM supplier_charges s WHERE s.booking_id = b.id)::bigint AS billed_minor"
            " FROM bookings b WHERE b.reference = :r"), {"r": reference}).mappings().one_or_none()
    return Settlement(**row) if row else None


def record_issue(reference: str, actor: finance.Actor, tickets: dict[int, str], *, supplier: SupplierSettlement | None = None,
                 document_key: str | None = None, document_type: str | None = None, note: str | None = None) -> None:
    supplier = supplier or SupplierSettlement()
    if supplier.paid_from is not None and supplier.paid_from not in PAID_FROM:
        raise SettlementRefused(422, "Choose how the supplier was paid: bank, supplier wallet or credit.", "invalid_paid_from")
    if supplier.billed_minor is not None and supplier.billed_minor <= 0:
        raise SettlementRefused(422, "Enter the amount the supplier billed.", "invalid_amount")
    with agencies.application_engine().begin() as connection:
        target = _load(connection, reference, lock=True)
        if target.product != "flight":
            raise SettlementRefused(409, "Only flight bookings are marked as issued here. Group tickets are recorded on the Tickets tab.", "not_flight")
        if target.test_booking:
            raise SettlementRefused(409, "This booking is set to \"Do not issue\" (a traveler is named \"Test\", or staff marked it).", "test_booking")
        if target.issuance_status == "issued":
            raise SettlementRefused(409, "This booking is already issued.", "already_issued")
        if target.issuance_status not in ("not_issued", "requested"):
            raise SettlementRefused(409, "Issuing is in progress or unknown for this booking. Resolve that first.", "issuance_open")
        if target.booking_status not in RECORDABLE_STATUSES:
            raise SettlementRefused(409, "This booking is closed, so it can't be marked as issued.", "closed")
        positions = {position for position, _ in target.traveler_types}
        if not positions or set(tickets) != positions:
            raise SettlementRefused(422, "Give a ticket number for every traveler.", "tickets_required")

        connection.execute(text(
            "UPDATE bookings SET issuance_status = 'issued', booking_status = 'ticket_issued', supplier_paid_from = :from,"
            " supplier_payment_reference = :ref, supplier_paid_at = :at, ticket_document_key = :key, ticket_document_type = :type,"
            " updated_at = now(), revision = revision + 1 WHERE id = :b"),
            {"from": supplier.paid_from, "ref": supplier.reference, "at": supplier.paid_at, "key": document_key, "type": document_type, "b": target.id})
        for position, number in tickets.items():
            connection.execute(text("UPDATE booking_travelers SET ticket_number = :t WHERE booking_id = :b AND position = :p"),
                               {"t": number, "b": target.id, "p": position})
        _event(connection, target.id, actor.user_id, "issuance_recorded_offline", {
            "tickets": len(tickets), "previous_booking_status": target.booking_status, "note": note,
            "ticket_document": bool(document_key),  # how the supplier was paid stays out: the Audit tab is shown to every role
        })

        charge = supplier.billed_minor
        if charge is None:
            finance.record_issue_charges(connection, target.id, actor.user_id)
        elif not connection.scalar(text("SELECT 1 FROM supplier_charges WHERE booking_id = :b AND source = 'issuance' LIMIT 1"), {"b": target.id}):
            charge_id = str(uuid4())
            connection.execute(text(
                "INSERT INTO supplier_charges (id, booking_id, traveler_id, provider_id, amount_minor, currency, source, supplier_reference, reason,"
                " recorded_by_user_id) VALUES (:id, :b, NULL, :p, :a, :c, 'issuance', :sref, :reason, :u)"),
                {"id": charge_id, "b": target.id, "p": target.provider, "a": charge, "c": target.currency, "sref": supplier.reference,
                 "reason": "Billed amount confirmed by the supplier", "u": actor.user_id})
            finance.audit(connection, actor, "supplier_charge_recorded", "supplier_charge", charge_id, booking_id=target.id,
                          before={"hold_cost_minor": target.supplier_cost_minor},
                          after={"amount_minor": charge, "currency": target.currency, "source": "issuance", "paid_from": supplier.paid_from})
        if charge is None:
            charge = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0)::bigint FROM supplier_charges WHERE booking_id = :b"),
                                       {"b": target.id}) or None

        if supplier.paid_from == "bank" and charge and supplier_wallets.SUPPLIERS.get(target.provider or "", ("", ""))[1] == "calculated":
            # Fresh money for this ticket: paid in and spent at once, so the calculated balance doesn't move.
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:p))"), {"p": f"wallet:{target.provider}"})
            entry_id = str(uuid4())
            connection.execute(text(
                "INSERT INTO supplier_wallet_entries (id, provider_id, kind, amount_minor, currency, reference, reason, occurred_at, recorded_by_user_id)"
                " VALUES (:id, :p, 'top_up', :a, 'PKR', :ref, :reason, COALESCE(:at, now()), :u)"),
                {"id": entry_id, "p": target.provider, "a": charge, "ref": supplier.reference or f"{target.reference} ticket",
                 "reason": f"Paid from the bank for {target.reference}", "at": supplier.paid_at, "u": actor.user_id})
            finance.audit(connection, actor, "supplier_top_up_recorded", "supplier_wallet", target.provider, booking_id=target.id,
                          after={"amount_minor": charge, "reference": supplier.reference, "booking": target.reference},
                          reason=f"Paid from the bank for {target.reference}")
        if supplier.paid_from or supplier.billed_minor is not None:
            finance.audit(connection, actor, "supplier_payment_recorded", "supplier_charge", target.reference, booking_id=target.id,
                          after={"billed_minor": charge, "paid_from": supplier.paid_from, "reference": supplier.reference,
                                 "paid_at": supplier.paid_at.isoformat() if supplier.paid_at else None})


def attach_document(reference: str, actor: finance.Actor, document_key: str, document_type: str) -> None:
    """Attach (or replace) the final ticket document on an issued flight booking."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("SELECT id, product, issuance_status, ticket_document_key FROM bookings WHERE reference = :r FOR UPDATE"),
                                 {"r": reference}).mappings().one_or_none()
        if row is None:
            raise SettlementRefused(404, "Booking not found.", "not_found")
        if row["issuance_status"] != "issued":
            raise SettlementRefused(409, "Attach the ticket document once the booking is issued.", "not_issued")
        connection.execute(text("UPDATE bookings SET ticket_document_key = :k, ticket_document_type = :t, updated_at = now(), revision = revision + 1"
                                " WHERE id = :b"), {"k": document_key, "t": document_type, "b": row["id"]})
        _event(connection, row["id"], actor.user_id, "ticket_document_attached", {"replaced": bool(row["ticket_document_key"])})


@dataclass(frozen=True)
class Settled:
    waived_minor: int
    total_minor: int
    currency: str


def settle_due(reference: str, actor: finance.Actor, reason: str) -> Settled:
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise SettlementRefused(422, "Say why the rest isn't being collected (what was agreed, and with whom).", "reason_required")
    with agencies.application_engine().begin() as connection:
        booking = connection.execute(text("SELECT id, booking_status, currency, total_minor FROM bookings WHERE reference = :r FOR UPDATE"),
                                     {"r": reference}).mappings().one_or_none()
        if booking is None:
            raise SettlementRefused(404, "Booking not found.", "not_found")
        if booking["booking_status"] in ("cancelled", "hold_pending", "hold_failed", "hold_unrecorded"):
            raise SettlementRefused(409, "This booking is closed, so its due can't be settled.", "closed")
        paid = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0)::bigint FROM booking_payments WHERE booking_id = :b"), {"b": booking["id"]})
        if paid <= 0:
            raise SettlementRefused(409, "Record the payment received first. A booking with nothing paid can't be settled.", "nothing_paid")
        waived = (booking["total_minor"] or 0) - paid
        if waived <= 0:
            raise SettlementRefused(409, "Nothing is due on this booking.", "nothing_due")
        connection.execute(text(
            "UPDATE bookings SET total_minor = :paid, payment_status = 'paid', settled_waived_minor = :w,"
            " settled_original_total_minor = :total, settled_reason = :reason, settled_at = now(), settled_by_user_id = :u,"
            " updated_at = now(), revision = revision + 1 WHERE id = :b"),
            {"paid": paid, "w": waived, "total": booking["total_minor"], "reason": reason, "u": actor.user_id, "b": booking["id"]})
        details = {"waived_minor": waived, "previous_total_minor": booking["total_minor"], "total_minor": paid, "currency": booking["currency"],
                   "reason": reason}
        _event(connection, booking["id"], actor.user_id, "due_settled", details)
        finance.audit(connection, actor, "due_settled", "booking", reference, booking_id=booking["id"],
                      before={"total_minor": booking["total_minor"], "due_minor": waived}, after={"total_minor": paid, "due_minor": 0}, reason=reason)
        return Settled(waived, paid, booking["currency"])
