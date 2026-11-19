"""Guest (website) bookings and PayFast attempts (migration 0022).

A guest reaches their booking only with its reference and the private token issued when it was made; only the
token's SHA-256 is stored. PayFast payments are recorded only from a verified PayFast result, once per attempt,
in the same transaction that locks the attempt and the booking.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies
from app.db.booking_payments import payment_status
from app.db.supplier_bookings import _event

PAYFAST_LABEL = "PayFast"


def new_token() -> tuple[str, str]:
    """(token for the guest, hash to store)."""
    token = secrets.token_urlsafe(24)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mark_website(booking_id: str, guest_hash: str) -> None:
    with agencies.application_engine().begin() as connection:
        connection.execute(text("UPDATE bookings SET channel = 'website', guest_token_hash = :h WHERE id = :id"),
                           {"h": guest_hash, "id": booking_id})


def mark_website_reference(reference: str, guest_hash: str) -> None:
    """mark_website for any product (flight requests), by reference."""
    with agencies.application_engine().begin() as connection:
        connection.execute(text("UPDATE bookings SET channel = 'website', guest_token_hash = :h WHERE reference = :r"),
                           {"h": guest_hash, "r": reference})


def summary(reference: str) -> dict | None:
    """A website booking of any product, for the guest's view: statuses, totals, paid amount and itinerary."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "SELECT b.id, b.reference, b.product, b.pnr, b.booking_status, b.payment_status, b.issuance_status, b.hold_expires_at,"
            " b.currency, b.total_minor, b.itinerary, b.supplier_response ->> 'final_pnr' AS final_pnr,"
            " COALESCE((SELECT SUM(p.amount_minor) FROM booking_payments p WHERE p.booking_id = b.id), 0) AS paid_minor"
            " FROM bookings b WHERE b.reference = :r"), {"r": reference}).mappings().one_or_none()
    return dict(row) if row else None


def guest_can_open(reference: str, token: str) -> bool:
    if not token or len(token) > 200:
        return False
    with agencies.application_engine().begin() as connection:
        stored = connection.scalar(text("SELECT guest_token_hash FROM bookings WHERE reference = :r AND channel = 'website'"),
                                   {"r": reference})
    return bool(stored) and hmac.compare_digest(stored, token_hash(token))


@dataclass(frozen=True)
class Attempt:
    id: str
    booking_id: str
    reference: str
    basket_id: str
    amount_minor: int
    currency: str
    status: str
    transaction_id: str | None


def create_attempt(reference: str, basket_id: str, amount_minor: int, currency: str) -> Attempt:
    with agencies.application_engine().begin() as connection:
        booking_id = connection.scalar(text("SELECT id FROM bookings WHERE reference = :r"), {"r": reference})
        attempt_id = str(uuid4())
        connection.execute(
            text("INSERT INTO payfast_attempts (id, booking_id, basket_id, amount_minor, currency) VALUES (:id, :b, :basket, :a, :c)"),
            {"id": attempt_id, "b": booking_id, "basket": basket_id, "a": amount_minor, "c": currency},
        )
        _event(connection, booking_id, None, "online_payment_started",
               {"provider": "payfast", "basket_id": basket_id, "amount_minor": amount_minor, "currency": currency})
    return Attempt(attempt_id, booking_id, reference, basket_id, amount_minor, currency, "initiated", None)


@dataclass(frozen=True)
class Outcome:
    reference: str
    status: str  # paid | failed | already_paid | already_failed | amount_mismatch | unknown_basket
    payment_status: str | None = None


def record_result(basket_id: str, *, paid: bool, err_code: str, transaction_id: str | None, amount_minor: int | None,
                  message: str | None, source: str, now: datetime) -> Outcome:
    """Apply one verified PayFast result. Idempotent: an attempt changes state once; later copies are ignored."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(
            text("SELECT a.*, b.reference, b.total_minor, b.currency AS booking_currency, b.booking_status,"
                 " b.hold_expires_at <= :now AS hold_over"
                 " FROM payfast_attempts a JOIN bookings b ON b.id = a.booking_id WHERE a.basket_id = :basket FOR UPDATE OF a, b"),
            {"basket": basket_id, "now": now},
        ).mappings().one_or_none()
        if row is None:
            return Outcome("", "unknown_basket")
        if row["status"] != "initiated":
            return Outcome(row["reference"], "already_paid" if row["status"] == "paid" else "already_failed")
        if not paid:
            connection.execute(text("UPDATE payfast_attempts SET status = 'failed', err_code = :e, transaction_id = :t, updated_at = now()"
                                    " WHERE id = :id"), {"e": err_code[:8], "t": transaction_id, "id": row["id"]})
            _event(connection, row["booking_id"], None, "online_payment_failed",
                   {"provider": "payfast", "basket_id": basket_id, "err_code": err_code[:8], "message": (message or "")[:200], "source": source})
            return Outcome(row["reference"], "failed")
        if amount_minor is not None and amount_minor != row["amount_minor"]:
            # Genuine PayFast result for a different amount than we asked: not recorded as a payment; staff check it.
            connection.execute(text("UPDATE payfast_attempts SET err_code = :e, transaction_id = :t, updated_at = now() WHERE id = :id"),
                               {"e": err_code[:8], "t": transaction_id, "id": row["id"]})
            _event(connection, row["booking_id"], None, "online_payment_needs_review",
                   {"provider": "payfast", "basket_id": basket_id, "reason": "amount_mismatch", "expected_minor": row["amount_minor"],
                    "reported_minor": amount_minor, "transaction_id": transaction_id, "source": source})
            return Outcome(row["reference"], "amount_mismatch")
        connection.execute(text("UPDATE payfast_attempts SET status = 'paid', err_code = :e, transaction_id = :t, updated_at = now() WHERE id = :id"),
                           {"e": err_code[:8], "t": transaction_id, "id": row["id"]})
        payment_id = str(uuid4())
        connection.execute(
            text("INSERT INTO booking_payments (id, booking_id, amount_minor, currency, method, received_into, account_id, reference,"
                 " received_at, recorded_by_user_id) VALUES (:id, :b, :amount, :currency, 'payfast', :into, NULL, :ref, :at, NULL)"),
            {"id": payment_id, "b": row["booking_id"], "amount": row["amount_minor"], "currency": row["currency"],
             "into": PAYFAST_LABEL, "ref": (transaction_id or basket_id)[:64], "at": now},
        )
        paid_total = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments WHERE booking_id = :b"),
                                       {"b": row["booking_id"]})
        status = payment_status(paid_total, row["total_minor"])
        connection.execute(text("UPDATE bookings SET payment_status = :s, updated_at = now(), revision = revision + 1 WHERE id = :b"),
                           {"s": status, "b": row["booking_id"]})
        review = []
        if row["booking_status"] not in ("held", "pending_payment", "confirmed") or row["hold_over"]:
            review.append("paid_after_hold_ended")
        if paid_total > row["total_minor"]:
            review.append("overpaid")
        _event(connection, row["booking_id"], None, "payment_recorded", {
            "amount_minor": row["amount_minor"], "currency": row["currency"], "method": "payfast", "received_into": PAYFAST_LABEL,
            "payer_reference": transaction_id or basket_id, "payment_status": status, "source": source, "needs_review": review,
        })
    return Outcome(row["reference"], "paid", status)


def latest_attempt(reference: str) -> Attempt | None:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(
            text("SELECT a.*, b.reference FROM payfast_attempts a JOIN bookings b ON b.id = a.booking_id"
                 " WHERE b.reference = :r ORDER BY a.created_at DESC LIMIT 1"), {"r": reference},
        ).mappings().one_or_none()
    if row is None:
        return None
    return Attempt(row["id"], row["booking_id"], row["reference"], row["basket_id"], row["amount_minor"], row["currency"],
                   row["status"], row["transaction_id"])


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def lookup(reference: str, contact: str) -> dict | None:
    """The booking when `contact` is the email or phone it was booked with (any booking, any channel), else None."""
    contact = contact.strip()
    with agencies.application_engine().begin() as connection:
        row = connection.execute(
            text("SELECT b.id, b.reference, b.product, b.booking_status, b.payment_status, b.issuance_status, b.hold_expires_at,"
                 " b.currency, b.total_minor, b.pnr, b.itinerary, b.supplier_response ->> 'final_pnr' AS final_pnr,"
                 " c.email, c.phone_code, c.phone,"
                 " (SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments p WHERE p.booking_id = b.id) AS paid_minor"
                 " FROM bookings b JOIN booking_customers c ON c.booking_id = b.id WHERE b.reference = :r"),
            {"r": reference.strip().upper()},
        ).mappings().one_or_none()
        if row is None:
            return None
        by_email = "@" in contact and contact.lower() == (row["email"] or "").strip().lower()
        given, stored = _digits(contact), _digits(f"{row['phone_code']}{row['phone']}")
        by_phone = len(given) >= 7 and (stored.endswith(given[-10:]) or _digits(row["phone"]).endswith(given[-10:]))
        if not (by_email or by_phone):
            return None
        travelers = connection.execute(
            text("SELECT title, first_name, last_name, ticket_number FROM booking_travelers WHERE booking_id = :b ORDER BY position"),
            {"b": row["id"]},
        ).mappings().all()
    return {**dict(row), "travelers": [dict(t) for t in travelers]}
