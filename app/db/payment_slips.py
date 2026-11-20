"""Payment slips (migration 0029, gateway#56, travelian#69/#76): a customer's "I've paid" with the slip file.

awaiting_check -> confirmed (the payment is recorded in the same transaction, app.db.booking_payments.record_in)
               -> rejected (with a reason). Booking, amount, method and file never change (database trigger).
The file lives in private S3 (payment-proof/); only its key is stored here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies, booking_payments, finance, payment_accounts

METHODS = ("bank_transfer", "jazzcash")
OPEN_FOR_SLIPS = ("held", "pending_payment", "confirmed", "requested")


class SlipRefused(Exception):
    def __init__(self, status: int, message: str, code: str = "refused"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


@dataclass(frozen=True)
class Slip:
    id: str
    booking_reference: str
    method: str
    account_id: str | None
    account_label: str | None
    amount_minor: int
    currency: str
    payer_reference: str | None
    asset_key: str
    content_type: str
    size_bytes: int
    status: str
    submitted_via: str
    submitted_at: datetime
    reviewed_by_user_id: str | None
    reviewed_at: datetime | None
    rejection_reason: str | None
    payment_id: str | None


_SELECT = ("SELECT s.*, b.reference AS booking_reference, a.label AS account_label FROM payment_slips s JOIN bookings b ON b.id = s.booking_id"
           " LEFT JOIN payment_accounts a ON a.id = s.account_id")


def _slip(r) -> Slip:
    return Slip(r["id"], r["booking_reference"], r["method"], r["account_id"], r["account_label"], r["amount_minor"], r["currency"],
                r["payer_reference"], r["asset_key"], r["content_type"], r["size_bytes"], r["status"], r["submitted_via"], r["submitted_at"],
                r["reviewed_by_user_id"], r["reviewed_at"], r["rejection_reason"], r["payment_id"])


def chosen_account(reference: str) -> str | None:
    with agencies.application_engine().begin() as connection:
        return connection.scalar(text("SELECT chosen_account_id FROM bookings WHERE reference = :r"), {"r": reference})


def choose_account(reference: str, account_id: str) -> str:
    """The customer picked the account they will pay into (shown to staff, used for matching). Returns its method."""
    with agencies.application_engine().begin() as connection:
        account = next((a for a in payment_accounts.list_accounts() if a.id == account_id and a.method in METHODS), None)
        if account is None:
            raise SlipRefused(422, "Choose one of the accounts shown.", "unknown_account")
        connection.execute(text("UPDATE bookings SET chosen_account_id = :a, updated_at = now() WHERE reference = :r"),
                           {"a": account_id, "r": reference})
    return account.method


def submit(reference: str, *, method: str, account_id: str | None, amount_minor: int, payer_reference: str | None, asset_key: str,
           content_type: str, size_bytes: int, via: str) -> Slip:
    if method not in METHODS:
        raise SlipRefused(422, "Slips are for bank transfer and JazzCash payments.", "invalid_method")
    if amount_minor <= 0:
        raise SlipRefused(422, "Enter the amount you paid.", "invalid_amount")
    with agencies.application_engine().begin() as connection:
        booking = connection.execute(text(
            "SELECT b.id, b.currency, b.total_minor, b.booking_status,"
            " (SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments p WHERE p.booking_id = b.id)::bigint AS paid"
            " FROM bookings b WHERE b.reference = :r FOR UPDATE"), {"r": reference}).mappings().one_or_none()
        if booking is None:
            raise SlipRefused(404, "Booking not found.", "not_found")
        if booking["booking_status"] not in OPEN_FOR_SLIPS:
            raise SlipRefused(409, "This booking can't take a payment now. Contact Travelian.", "closed")
        due = booking["total_minor"] - booking["paid"]
        if amount_minor > due:
            raise SlipRefused(409, f"That is more than the amount due ({due // 100:,} {booking['currency']}).", "more_than_due")
        if account_id is not None:
            account = next((a for a in payment_accounts.list_accounts() if a.id == account_id and a.method == method), None)
            if account is None:
                raise SlipRefused(422, "Choose the account you paid into.", "unknown_account")
            connection.execute(text("UPDATE bookings SET chosen_account_id = :a WHERE id = :b"), {"a": account_id, "b": booking["id"]})
        slip_id = str(uuid4())
        connection.execute(text(
            "INSERT INTO payment_slips (id, booking_id, method, account_id, amount_minor, currency, payer_reference, asset_key, content_type,"
            " size_bytes, submitted_via) VALUES (:id, :b, :m, :a, :amt, :c, :ref, :key, :type, :size, :via)"),
            {"id": slip_id, "b": booking["id"], "m": method, "a": account_id, "amt": amount_minor, "c": booking["currency"],
             "ref": (payer_reference or "").strip()[:64] or None, "key": asset_key, "type": content_type, "size": size_bytes, "via": via})
        from app.db.supplier_bookings import _event

        _event(connection, booking["id"], None, "payment_slip_uploaded", {"method": method, "amount_minor": amount_minor, "via": via})
        return _slip(connection.execute(text(_SELECT + " WHERE s.id = :id"), {"id": slip_id}).mappings().one())


def get(slip_id: str) -> Slip:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(_SELECT + " WHERE s.id = :id"), {"id": slip_id}).mappings().one_or_none()
    if row is None:
        raise SlipRefused(404, "Payment slip not found.", "not_found")
    return _slip(row)


def for_booking(reference: str) -> list[Slip]:
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text(_SELECT + " WHERE b.reference = :r ORDER BY s.submitted_at"), {"r": reference}).mappings().all()
    return [_slip(r) for r in rows]


def confirm(slip_id: str, actor: finance.Actor, *, amount_minor: int, account_id: str, payer_reference: str | None,
            received_at: datetime) -> Slip:
    """Staff checked the money arrived: the payment is recorded and the slip confirmed in one transaction. Staff may
    correct the amount, account and reference to what actually arrived (the slip keeps what the customer said)."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(_SELECT + " WHERE s.id = :id FOR UPDATE OF s"), {"id": slip_id}).mappings().one_or_none()
        if row is None:
            raise SlipRefused(404, "Payment slip not found.", "not_found")
        if row["status"] != "awaiting_check":
            raise SlipRefused(409, f"This slip is already {row['status'].replace('_', ' ')}.", "wrong_status")
        try:
            payment, status = booking_payments.record_in(
                connection, row["booking_reference"], amount_minor=amount_minor, currency=row["currency"], method=row["method"],
                account_id=account_id, payer_reference=(payer_reference or "").strip() or row["payer_reference"] or None,
                received_at=received_at, actor_user_id=actor.user_id, extra_event={"payment_slip_id": slip_id})
        except booking_payments.PaymentRefused as error:
            raise SlipRefused(error.status, error.message, "payment_refused") from None
        connection.execute(text("UPDATE payment_slips SET status = 'confirmed', payment_id = :p, reviewed_by_user_id = :u, reviewed_at = clock_timestamp()"
                                " WHERE id = :id"), {"p": payment.id, "u": actor.user_id, "id": slip_id})
        finance.audit(connection, actor, "payment_slip_confirmed", "payment_slip", slip_id, booking_id=row["booking_id"],
                      before={"status": "awaiting_check", "claimed_amount_minor": row["amount_minor"]},
                      after={"status": "confirmed", "amount_minor": amount_minor, "payment_id": payment.id, "payment_status": status})
        return _slip(connection.execute(text(_SELECT + " WHERE s.id = :id"), {"id": slip_id}).mappings().one())


def reject(slip_id: str, actor: finance.Actor, reason: str) -> Slip:
    if not reason or not reason.strip():
        raise SlipRefused(422, "Say why the slip is rejected (the customer may be told).", "reason_required")
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(_SELECT + " WHERE s.id = :id FOR UPDATE OF s"), {"id": slip_id}).mappings().one_or_none()
        if row is None:
            raise SlipRefused(404, "Payment slip not found.", "not_found")
        if row["status"] != "awaiting_check":
            raise SlipRefused(409, f"This slip is already {row['status'].replace('_', ' ')}.", "wrong_status")
        connection.execute(text("UPDATE payment_slips SET status = 'rejected', rejection_reason = :r, reviewed_by_user_id = :u,"
                                " reviewed_at = clock_timestamp() WHERE id = :id"), {"r": reason.strip(), "u": actor.user_id, "id": slip_id})
        finance.audit(connection, actor, "payment_slip_rejected", "payment_slip", slip_id, booking_id=row["booking_id"],
                      before={"status": "awaiting_check"}, after={"status": "rejected"}, reason=reason.strip())
        return _slip(connection.execute(text(_SELECT + " WHERE s.id = :id"), {"id": slip_id}).mappings().one())
