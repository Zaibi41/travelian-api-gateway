"""Finance records (migration 0028, Zaibi41/travelian#91): audit log, payment fees, supplier charges, refunds, settings.

Rules:
- Integer paisa only. Nothing here moves money: a refund marked "paid" records a payout staff already made.
- Money records are append-only (database triggers). A correction is a new row with a reason.
- Every action writes a finance_audit_log row in the same transaction: who, when, what, before, after, reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies

REFUND_METHODS = ("bank_transfer", "jazzcash", "1link", "payfast")
FEE_SOURCES = ("recorded", "estimated", "settlement")


class FinanceRefused(Exception):
    def __init__(self, status: int, message: str, code: str = "refused"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Actor:
    user_id: str | None
    role: str | None


SYSTEM = Actor(None, "system")


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, sort_keys=True)


def audit(connection, actor: Actor, action: str, entity_type: str, entity_id: str | None, *, booking_id: str | None = None,
          before: Any = None, after: Any = None, reason: str | None = None) -> None:
    connection.execute(
        text("INSERT INTO finance_audit_log (actor_user_id, actor_role, action, entity_type, entity_id, booking_id, before, after, reason)"
             " VALUES (:u, :r, :a, :t, :e, :b, CAST(:before AS jsonb), CAST(:after AS jsonb), :reason)"),
        {"u": actor.user_id, "r": actor.role, "a": action, "t": entity_type, "e": entity_id, "b": booking_id,
         "before": _json(before), "after": _json(after), "reason": reason},
    )


# ---------------------------------------------------------------------------------------------------------------- audit log
@dataclass(frozen=True)
class AuditEntry:
    id: int
    at: datetime
    actor_user_id: str | None
    actor_role: str | None
    action: str
    entity_type: str
    entity_id: str | None
    booking_reference: str | None
    before: Any
    after: Any
    reason: str | None


def list_audit(*, action: str | None = None, entity_type: str | None = None, booking_reference: str | None = None,
               date_from: datetime | None = None, date_to: datetime | None = None, limit: int = 50, offset: int = 0) -> tuple[list[AuditEntry], int]:
    where, params = ["true"], {"limit": limit, "offset": offset}
    if action:
        where.append("l.action = :action")
        params["action"] = action
    if entity_type:
        where.append("l.entity_type = :entity")
        params["entity"] = entity_type
    if booking_reference:
        where.append("upper(b.reference) = upper(:ref)")
        params["ref"] = booking_reference.strip()
    if date_from:
        where.append("l.at >= :from")
        params["from"] = date_from
    if date_to:
        where.append("l.at < :to")
        params["to"] = date_to
    clause = " AND ".join(where)
    with agencies.application_engine().begin() as connection:
        total = connection.scalar(text(f"SELECT count(*) FROM finance_audit_log l LEFT JOIN bookings b ON b.id = l.booking_id WHERE {clause}"), params)
        rows = connection.execute(text(
            "SELECT l.*, b.reference AS booking_reference FROM finance_audit_log l LEFT JOIN bookings b ON b.id = l.booking_id"
            f" WHERE {clause} ORDER BY l.at DESC, l.id DESC LIMIT :limit OFFSET :offset"), params).mappings().all()
    return [AuditEntry(r["id"], r["at"], r["actor_user_id"], r["actor_role"], r["action"], r["entity_type"], r["entity_id"],
                       r["booking_reference"], r["before"], r["after"], r["reason"]) for r in rows], total


# ---------------------------------------------------------------------------------------------------------------- bookings
@dataclass(frozen=True)
class BookingRef:
    id: str
    reference: str
    currency: str
    total_minor: int
    supplier_cost_minor: int
    cost_known: bool
    provider_id: str | None
    booking_status: str


def _booking(connection, reference: str, lock: bool = False) -> BookingRef:
    row = connection.execute(text(
        "SELECT id, reference, currency, total_minor, supplier_cost_minor, cost_known, provider_id, booking_status FROM bookings"
        " WHERE upper(reference) = upper(:r)" + (" FOR UPDATE" if lock else "")), {"r": reference.strip()}).mappings().one_or_none()
    if row is None:
        raise FinanceRefused(404, "Booking not found.", "not_found")
    return BookingRef(**row)


# ---------------------------------------------------------------------------------------------------------------- supplier charges
@dataclass(frozen=True)
class SupplierCharge:
    id: str
    traveler_position: int | None
    traveler_name: str | None
    provider_id: str | None
    amount_minor: int
    currency: str
    source: str
    supplier_reference: str | None
    reason: str | None
    recorded_by_user_id: str | None
    recorded_at: datetime


def record_issue_charges(connection, booking_id: str, actor_user_id: str | None) -> int:
    """At issue: the supplier's actual charge for each ticket is the booking's confirmed cost per traveler (the issuer
    confirms a changed airline price before issuing, app.db.issuance.update_cost; SooperFare's billable price is checked
    against it before the confirm). Recorded once per booking; a later re-issue or ticket edit adds nothing. Returns rows added."""
    if connection.scalar(text("SELECT 1 FROM supplier_charges WHERE booking_id = :b AND source = 'issuance' LIMIT 1"), {"b": booking_id}):
        return 0
    booking = connection.execute(text("SELECT currency, provider_id FROM bookings WHERE id = :b"), {"b": booking_id}).mappings().one()
    travelers = connection.execute(text("SELECT id, cost_minor FROM booking_travelers WHERE booking_id = :b ORDER BY position"),
                                   {"b": booking_id}).all()
    added = 0
    for traveler_id, cost in travelers:
        if not cost:
            continue
        charge_id = str(uuid4())
        connection.execute(text(
            "INSERT INTO supplier_charges (id, booking_id, traveler_id, provider_id, amount_minor, currency, source, recorded_by_user_id)"
            " VALUES (:id, :b, :t, :p, :a, :c, 'issuance', :u)"),
            {"id": charge_id, "b": booking_id, "t": traveler_id, "p": booking["provider_id"], "a": cost, "c": booking["currency"], "u": actor_user_id})
        audit(connection, Actor(actor_user_id, None if actor_user_id else "system"), "supplier_charge_recorded", "supplier_charge", charge_id,
              booking_id=booking_id, after={"amount_minor": cost, "currency": booking["currency"], "source": "issuance"})
        added += 1
    return added


def add_supplier_charge(reference: str, actor: Actor, *, amount_minor: int, reason: str, traveler_position: int | None = None,
                        supplier_reference: str | None = None) -> SupplierCharge:
    """Finance records (or corrects, with a negative amount) what the supplier charged. A reason is required."""
    if amount_minor == 0:
        raise FinanceRefused(422, "Enter an amount other than zero.", "invalid_amount")
    if not reason or not reason.strip():
        raise FinanceRefused(422, "Say why this charge is recorded.", "reason_required")
    with agencies.application_engine().begin() as connection:
        booking = _booking(connection, reference, lock=True)
        traveler_id = None
        if traveler_position is not None:
            traveler_id = connection.scalar(text("SELECT id FROM booking_travelers WHERE booking_id = :b AND position = :p"),
                                            {"b": booking.id, "p": traveler_position})
            if traveler_id is None:
                raise FinanceRefused(422, "That traveler isn't on this booking.", "unknown_traveler")
        total = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0)::bigint FROM supplier_charges WHERE booking_id = :b"), {"b": booking.id})
        if total + amount_minor < 0:
            raise FinanceRefused(409, "The charges on this booking can't go below zero.", "below_zero")
        charge_id = str(uuid4())
        connection.execute(text(
            "INSERT INTO supplier_charges (id, booking_id, traveler_id, provider_id, amount_minor, currency, source, supplier_reference, reason,"
            " recorded_by_user_id) VALUES (:id, :b, :t, :p, :a, :c, 'manual', :sref, :reason, :u)"),
            {"id": charge_id, "b": booking.id, "t": traveler_id, "p": booking.provider_id, "a": amount_minor, "c": booking.currency,
             "sref": (supplier_reference or "").strip() or None, "reason": reason.strip(), "u": actor.user_id})
        audit(connection, actor, "supplier_charge_recorded", "supplier_charge", charge_id, booking_id=booking.id,
              before={"charges_total_minor": total}, after={"amount_minor": amount_minor, "charges_total_minor": total + amount_minor,
                                                            "source": "manual"}, reason=reason.strip())
    return next(c for c in supplier_charges_for(booking.id) if c.id == charge_id)


def supplier_charges_for(booking_id: str, connection=None) -> list[SupplierCharge]:
    sql = text("SELECT s.*, t.position, NULLIF(TRIM(CONCAT(t.first_name, ' ', t.last_name)), '') AS traveler_name FROM supplier_charges s"
               " LEFT JOIN booking_travelers t ON t.id = s.traveler_id WHERE s.booking_id = :b ORDER BY s.recorded_at, s.id")

    def read(conn):
        return [SupplierCharge(r["id"], r["position"], r["traveler_name"], r["provider_id"], r["amount_minor"], r["currency"], r["source"],
                               r["supplier_reference"], r["reason"], r["recorded_by_user_id"], r["recorded_at"])
                for r in conn.execute(sql, {"b": booking_id}).mappings().all()]
    if connection is not None:
        return read(connection)
    with agencies.application_engine().begin() as conn:
        return read(conn)


# ---------------------------------------------------------------------------------------------------------------- payment fees
@dataclass(frozen=True)
class PaymentFee:
    id: str
    payment_id: str
    amount_minor: int
    currency: str
    source: str
    reason: str | None
    recorded_by_user_id: str | None
    recorded_at: datetime


def add_payment_fee(payment_id: str, actor: Actor, *, amount_minor: int, source: str = "recorded", reason: str | None = None,
                    connection=None) -> PaymentFee:
    """What a payment cost Travelian (e.g. PayFast's fee). Negative amounts correct an earlier entry."""
    if source not in FEE_SOURCES:
        raise FinanceRefused(422, "Unknown fee source.", "invalid_source")
    if amount_minor == 0:
        raise FinanceRefused(422, "Enter an amount other than zero.", "invalid_amount")
    if source == "recorded" and not (reason or "").strip():
        raise FinanceRefused(422, "Say where this fee comes from (for example the PayFast statement).", "reason_required")

    def write(conn) -> PaymentFee:
        payment = conn.execute(text("SELECT id, booking_id, currency FROM booking_payments WHERE id = :p FOR UPDATE"),
                               {"p": payment_id}).mappings().one_or_none()
        if payment is None:
            raise FinanceRefused(404, "Payment not found.", "not_found")
        total = conn.scalar(text("SELECT COALESCE(SUM(amount_minor), 0)::bigint FROM payment_fees WHERE payment_id = :p"), {"p": payment_id})
        if total + amount_minor < 0:
            raise FinanceRefused(409, "The fees on this payment can't go below zero.", "below_zero")
        fee_id = str(uuid4())
        conn.execute(text("INSERT INTO payment_fees (id, payment_id, amount_minor, currency, source, reason, recorded_by_user_id)"
                          " VALUES (:id, :p, :a, :c, :s, :r, :u)"),
                     {"id": fee_id, "p": payment_id, "a": amount_minor, "c": payment["currency"], "s": source,
                      "r": (reason or "").strip() or None, "u": actor.user_id})
        audit(conn, actor, "payment_fee_recorded", "payment_fee", fee_id, booking_id=payment["booking_id"],
              before={"fees_total_minor": total}, after={"amount_minor": amount_minor, "fees_total_minor": total + amount_minor, "source": source,
                                                         "payment_id": payment_id}, reason=(reason or "").strip() or None)
        row = conn.execute(text("SELECT * FROM payment_fees WHERE id = :id"), {"id": fee_id}).mappings().one()
        return _fee(row)
    if connection is not None:
        return write(connection)
    with agencies.application_engine().begin() as conn:
        return write(conn)


def _fee(r) -> PaymentFee:
    return PaymentFee(r["id"], r["payment_id"], r["amount_minor"], r["currency"], r["source"], r["reason"], r["recorded_by_user_id"], r["recorded_at"])


def fees_for(booking_id: str, connection) -> list[PaymentFee]:
    rows = connection.execute(text("SELECT f.* FROM payment_fees f JOIN booking_payments p ON p.id = f.payment_id WHERE p.booking_id = :b"
                                   " ORDER BY f.recorded_at, f.id"), {"b": booking_id}).mappings().all()
    return [_fee(r) for r in rows]


# ---------------------------------------------------------------------------------------------------------------- refunds
@dataclass(frozen=True)
class Refund:
    id: str
    booking_reference: str
    amount_minor: int
    currency: str
    method: str
    status: str
    reason: str
    payout_reference: str | None
    rejection_reason: str | None
    requested_by_user_id: str | None
    requested_at: datetime
    approved_by_user_id: str | None
    approved_at: datetime | None
    paid_by_user_id: str | None
    paid_at: datetime | None
    rejected_by_user_id: str | None
    rejected_at: datetime | None
    customer_name: str | None = None


_REFUND_SELECT = ("SELECT r.*, b.reference AS booking_reference, NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), '') AS customer_name"
                  " FROM refunds r JOIN bookings b ON b.id = r.booking_id LEFT JOIN booking_customers c ON c.booking_id = b.id")


def _refund(r) -> Refund:
    return Refund(r["id"], r["booking_reference"], r["amount_minor"], r["currency"], r["method"], r["status"], r["reason"], r["payout_reference"],
                  r["rejection_reason"], r["requested_by_user_id"], r["requested_at"], r["approved_by_user_id"], r["approved_at"],
                  r["paid_by_user_id"], r["paid_at"], r["rejected_by_user_id"], r["rejected_at"], r.get("customer_name"))


def refundable_minor(connection, booking_id: str) -> int:
    """What can still be refunded: the fare money received (booking_payments.amount_minor; a card fee is kept separately
    and is not refunded, travelian#74) minus refunds not rejected."""
    paid = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0)::bigint FROM booking_payments WHERE booking_id = :b"), {"b": booking_id})
    refunded = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0)::bigint FROM refunds WHERE booking_id = :b AND status <> 'rejected'"),
                                 {"b": booking_id})
    return paid - refunded


def request_refund(reference: str, actor: Actor, *, amount_minor: int, method: str, reason: str) -> Refund:
    if amount_minor <= 0:
        raise FinanceRefused(422, "Enter the refund amount.", "invalid_amount")
    if method not in REFUND_METHODS:
        raise FinanceRefused(422, "Choose how the money goes back to the customer.", "invalid_method")
    if not reason or not reason.strip():
        raise FinanceRefused(422, "Say why the customer is refunded.", "reason_required")
    with agencies.application_engine().begin() as connection:
        booking = _booking(connection, reference, lock=True)
        available = refundable_minor(connection, booking.id)
        if amount_minor > available:
            raise FinanceRefused(409, f"This is more than can be refunded on this booking ({available // 100:,} {booking.currency} left).",
                                 "more_than_paid")
        refund_id = str(uuid4())
        connection.execute(text("INSERT INTO refunds (id, booking_id, amount_minor, currency, method, reason, requested_by_user_id)"
                                " VALUES (:id, :b, :a, :c, :m, :r, :u)"),
                           {"id": refund_id, "b": booking.id, "a": amount_minor, "c": booking.currency, "m": method, "r": reason.strip(),
                            "u": actor.user_id})
        audit(connection, actor, "refund_requested", "refund", refund_id, booking_id=booking.id,
              after={"amount_minor": amount_minor, "currency": booking.currency, "method": method, "status": "requested"}, reason=reason.strip())
        return _refund(connection.execute(text(_REFUND_SELECT + " WHERE r.id = :id"), {"id": refund_id}).mappings().one())


def _move_refund(refund_id: str, actor: Actor, to: str, *, reason: str | None = None, payout_reference: str | None = None,
                 paid_at: datetime | None = None) -> Refund:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(_REFUND_SELECT + " WHERE r.id = :id FOR UPDATE OF r"), {"id": refund_id}).mappings().one_or_none()
        if row is None:
            raise FinanceRefused(404, "Refund not found.", "not_found")
        allowed = {"approved": ("requested",), "rejected": ("requested", "approved"), "paid": ("approved",)}[to]
        if row["status"] not in allowed:
            raise FinanceRefused(409, f"This refund is {row['status']} and can't be marked {to}.", "wrong_status")
        sets = {"approved": "approved_by_user_id = :u, approved_at = clock_timestamp()",
                "rejected": "rejected_by_user_id = :u, rejected_at = clock_timestamp(), rejection_reason = :reason",
                "paid": "paid_by_user_id = :u, paid_at = :paid_at, payout_reference = :pref"}[to]
        connection.execute(text(f"UPDATE refunds SET status = :to, {sets} WHERE id = :id"),
                           {"to": to, "u": actor.user_id, "id": refund_id, "reason": reason, "pref": payout_reference, "paid_at": paid_at})
        audit(connection, actor, f"refund_{to}", "refund", refund_id, booking_id=row["booking_id"], before={"status": row["status"]},
              after={"status": to, **({"payout_reference": payout_reference, "paid_at": paid_at} if to == "paid" else {})}, reason=reason)
        return _refund(connection.execute(text(_REFUND_SELECT + " WHERE r.id = :id"), {"id": refund_id}).mappings().one())


def approve_refund(refund_id: str, actor: Actor) -> Refund:
    return _move_refund(refund_id, actor, "approved")


def reject_refund(refund_id: str, actor: Actor, reason: str) -> Refund:
    if not reason or not reason.strip():
        raise FinanceRefused(422, "Say why the refund is rejected.", "reason_required")
    return _move_refund(refund_id, actor, "rejected", reason=reason.strip())


def mark_refund_paid(refund_id: str, actor: Actor, *, payout_reference: str, paid_at: datetime) -> Refund:
    """Records a payout staff already made outside the gateway (bank, JazzCash or PayFast portal). Moves no money."""
    if not payout_reference or not payout_reference.strip():
        raise FinanceRefused(422, "Enter the payout reference (bank or PayFast transaction).", "reference_required")
    return _move_refund(refund_id, actor, "paid", payout_reference=payout_reference.strip(), paid_at=paid_at)


def refunds_for(booking_id: str, connection) -> list[Refund]:
    return [_refund(r) for r in connection.execute(text(_REFUND_SELECT + " WHERE r.booking_id = :b ORDER BY r.requested_at"),
                                                   {"b": booking_id}).mappings().all()]


def list_refunds(status: str | None = None, limit: int = 100, offset: int = 0) -> tuple[list[Refund], int]:
    where, params = "", {"limit": limit, "offset": offset}
    if status:
        where, params["s"] = " WHERE r.status = :s", status
    with agencies.application_engine().begin() as connection:
        total = connection.scalar(text("SELECT count(*) FROM refunds r" + where), params)
        rows = connection.execute(text(_REFUND_SELECT + where + " ORDER BY r.requested_at DESC LIMIT :limit OFFSET :offset"), params).mappings().all()
    return [_refund(r) for r in rows], total


# ---------------------------------------------------------------------------------------------------------------- settings
#: Settings Finance can change, with their defaults. Values are validated by the caller (app.services.finance).
DEFAULT_SETTINGS: dict[str, Any] = {}


def get_settings(connection=None) -> dict[str, Any]:
    def read(conn):
        return {**DEFAULT_SETTINGS, **{k: v for k, v in conn.execute(text("SELECT key, value FROM finance_settings")).all()}}
    if connection is not None:
        return read(connection)
    with agencies.application_engine().begin() as conn:
        return read(conn)


def set_setting(key: str, value: Any, actor: Actor, reason: str | None = None) -> dict[str, Any]:
    with agencies.application_engine().begin() as connection:
        before = connection.scalar(text("SELECT value FROM finance_settings WHERE key = :k FOR UPDATE"), {"k": key})
        connection.execute(text("INSERT INTO finance_settings (key, value, updated_by_user_id, updated_at) VALUES (:k, CAST(:v AS jsonb), :u, now())"
                                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by_user_id = EXCLUDED.updated_by_user_id,"
                                " updated_at = now()"), {"k": key, "v": json.dumps(value), "u": actor.user_id})
        audit(connection, actor, "setting_changed", "finance_setting", key, before={"value": before if before is not None else DEFAULT_SETTINGS.get(key)},
              after={"value": value}, reason=reason)
        return get_settings(connection)


# ---------------------------------------------------------------------------------------------------------------- booking summary
@dataclass
class BookingFinance:
    booking: BookingRef
    payments: list[dict] = field(default_factory=list)
    fees: list[PaymentFee] = field(default_factory=list)
    charges: list[SupplierCharge] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    refundable_minor: int = 0


def booking_finance(reference: str) -> BookingFinance:
    with agencies.application_engine().begin() as connection:
        booking = _booking(connection, reference)
        payments = [dict(r) for r in connection.execute(text(
            "SELECT id, amount_minor, currency, method, received_into, reference, received_at FROM booking_payments WHERE booking_id = :b"
            " ORDER BY received_at"), {"b": booking.id}).mappings().all()]
        return BookingFinance(booking, payments, fees_for(booking.id, connection), supplier_charges_for(booking.id, connection),
                              refunds_for(booking.id, connection), refundable_minor(connection, booking.id))
