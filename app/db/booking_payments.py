"""Customer payments on a booking (migration 0013).

Recording locks the booking row, so two payments recorded at once can't both pass the
"not more than the amount due" check. The booking's payment_status is recomputed in the same
transaction (unpaid, part_paid, paid) and a "payment_recorded" event is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import agencies, payment_accounts
from app.db.supplier_bookings import _event

METHODS = ("bank_transfer", "jazzcash", "1link")
#: Bookings that can't take a payment.
CLOSED_STATUSES = ("cancelled", "expired", "hold_pending", "hold_failed", "hold_unrecorded")


@dataclass(frozen=True)
class Payment:
    id: str
    amount_minor: int
    currency: str
    method: str
    received_into: str
    reference: str
    received_at: datetime
    recorded_by_user_id: str | None
    recorded_by_role: str | None
    recorded_at: datetime


class PaymentRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def payment_status(paid_minor: int, total_minor: int) -> str:
    if paid_minor <= 0:
        return "unpaid"
    return "paid" if paid_minor >= total_minor else "part_paid"


def record(reference: str, *, amount_minor: int, currency: str, method: str, account_id: str, payer_reference: str | None,
           received_at: datetime, actor_user_id: str) -> tuple[Payment, str]:
    """Record one payment into one of Travelian's active receiving accounts of that method (migration
    0019; its name is kept as received_into). Without a payer reference (Group Ticketing payments carry
    none) a gateway reference "TRV-PAY-<8 chars>" is stored. Returns (payment, the booking's new payment status)."""
    with agencies.application_engine().begin() as connection:
        booking = connection.execute(
            text("SELECT id, product, currency, total_minor, booking_status, hold_expires_at <= now() AS hold_over,"
                 " COALESCE(supplier_response -> 'price_change' ->> 'accepted', 'true') = 'false' AS price_change_pending"
                 " FROM bookings WHERE reference = :r FOR UPDATE"), {"r": reference},
        ).mappings().one_or_none()
        if booking is None:
            raise PaymentRefused(404, "Booking not found.")
        if booking["booking_status"] in CLOSED_STATUSES:
            raise PaymentRefused(409, "This booking is closed and can't take a payment.")
        if booking["booking_status"] in ("held", "pending_payment") and booking["hold_over"]:
            # A hold past its time limit takes no payment, even before the expiry job marks it (Flights and Group).
            raise PaymentRefused(409, "The hold has expired. Search again and hold new seats.")
        if booking["price_change_pending"]:
            raise PaymentRefused(409, "The supplier held the seats at a different price. Confirm the new price with the customer first.")
        if currency.upper() != booking["currency"]:
            raise PaymentRefused(422, f"Payments for this booking are in {booking['currency']}.")
        account = payment_accounts.active_for_payment(connection, account_id, method)
        if account is None:
            raise PaymentRefused(422, "Choose one of Travelian's receiving accounts for this payment method.")
        received_into = account.label
        paid = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments WHERE booking_id = :b"), {"b": booking["id"]})
        if paid + amount_minor > booking["total_minor"]:
            raise PaymentRefused(409, "This is more than the amount due on the booking.")
        payment_id = str(uuid4())
        payer_reference = payer_reference or f"TRV-PAY-{payment_id[:8].upper()}"
        try:
            with connection.begin_nested():
                connection.execute(
                    text("INSERT INTO booking_payments (id, booking_id, amount_minor, currency, method, received_into, account_id, reference,"
                         " received_at, recorded_by_user_id) VALUES (:id, :b, :amount, :currency, :method, :into, :account, :ref, :at, :actor)"),
                    {"id": payment_id, "b": booking["id"], "amount": amount_minor, "currency": booking["currency"], "method": method,
                     "into": received_into, "account": account.id, "ref": payer_reference, "at": received_at, "actor": actor_user_id},
                )
        except IntegrityError:
            raise PaymentRefused(409, "A payment with this method and reference is already recorded.") from None
        status = payment_status(paid + amount_minor, booking["total_minor"])
        connection.execute(
            text("UPDATE bookings SET payment_status = :s, updated_at = now(), revision = revision + 1 WHERE id = :b"),
            {"s": status, "b": booking["id"]},
        )
        _event(connection, booking["id"], actor_user_id, "payment_recorded", {
            "amount_minor": amount_minor, "currency": booking["currency"], "method": method, "received_into": received_into,
            "payer_reference": payer_reference, "payment_status": status,
        })
        row = connection.execute(
            text("SELECT p.*, r.name AS recorded_by_role FROM booking_payments p LEFT JOIN users u ON u.id = p.recorded_by_user_id"
                 " LEFT JOIN roles r ON r.id = u.role_id WHERE p.id = :id"), {"id": payment_id},
        ).mappings().one()
    return _payment(row), status


def _payment(row) -> Payment:
    return Payment(row["id"], row["amount_minor"], row["currency"], row["method"], row["received_into"], row["reference"],
                   row["received_at"], row["recorded_by_user_id"], row["recorded_by_role"], row["recorded_at"])


def list_for(connection, booking_id: str) -> list[Payment]:
    rows = connection.execute(
        text("SELECT p.*, r.name AS recorded_by_role FROM booking_payments p LEFT JOIN users u ON u.id = p.recorded_by_user_id"
             " LEFT JOIN roles r ON r.id = u.role_id WHERE p.booking_id = :b ORDER BY p.received_at, p.recorded_at"),
        {"b": booking_id},
    ).mappings().all()
    return [_payment(row) for row in rows]
