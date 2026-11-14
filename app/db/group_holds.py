"""Group Ticketing holds in `bookings` (product 'group'), with audit entries in `booking_events`.

A group hold is created by supplier_bookings.save_hold(product="group"). Its state changes here
lock the booking row and check the current state in the same transaction:
- expire: a held booking whose hold time has passed becomes 'expired' (it is never held again).
- release: a held booking becomes 'cancelled' after the supplier confirmed the release.
- accept_price: the customer agreed to the re-checked price; the total and supplier cost change.
supplier_ref and supplier_response never leave the gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.db import agencies
from app.db.booking_payments import payment_status
from app.db.supplier_bookings import _event


class GroupHoldRefused(Exception):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass(frozen=True)
class GroupBooking:
    id: str
    reference: str
    pnr: str | None
    provider: str | None
    booking_status: str
    issuance_status: str
    payment_status: str
    hold_expires_at: datetime | None
    currency: str
    total_minor: int
    supplier_cost_minor: int | None
    supplier_ref: str | None
    itinerary: list[dict[str, Any]]
    adults: int
    paid_minor: int

    def __repr__(self) -> str:  # never print the supplier reference
        return f"GroupBooking({self.reference}, {self.booking_status})"


def _load(connection, reference: str, lock: bool) -> GroupBooking:
    row = connection.execute(
        text("SELECT id, reference, pnr, provider_id, booking_status, issuance_status, payment_status, hold_expires_at, currency,"
             " total_minor, supplier_cost_minor, supplier_ref, itinerary, supplier_response -> 'adults' AS adults"
             " FROM bookings WHERE reference = :r AND product = 'group'" + (" FOR UPDATE" if lock else "")),
        {"r": reference},
    ).mappings().one_or_none()
    if row is None:
        raise GroupHoldRefused(404, "Group booking not found.", "not_found")
    paid = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments WHERE booking_id = :b"), {"b": row["id"]})
    adults = row["adults"] if isinstance(row["adults"], int) else connection.scalar(
        text("SELECT count(*) FROM booking_travelers WHERE booking_id = :b"), {"b": row["id"]})
    return GroupBooking(row["id"], row["reference"], row["pnr"], row["provider_id"], row["booking_status"], row["issuance_status"],
                        row["payment_status"], row["hold_expires_at"], row["currency"], row["total_minor"], row["supplier_cost_minor"],
                        row["supplier_ref"], row["itinerary"] if isinstance(row["itinerary"], list) else [], int(adults or 0), int(paid))


def load(reference: str) -> GroupBooking:
    with agencies.application_engine().begin() as connection:
        return _load(connection, reference, lock=False)


def is_over(booking: GroupBooking, now: datetime) -> bool:
    return booking.hold_expires_at is not None and booking.hold_expires_at <= now


def expire_if_over(reference: str, now: datetime) -> GroupBooking:
    """Mark a held booking whose time limit has passed as expired (once, with an audit entry).
    Leaves a booking that is being issued, or whose issuing is unclear, alone."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if booking.booking_status == "held" and is_over(booking, now) and booking.issuance_status in ("not_issued", "requested"):
            connection.execute(text("UPDATE bookings SET booking_status = 'expired', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                               {"b": booking.id})
            _event(connection, booking.id, None, "hold_expired", {"pnr": booking.pnr})
            booking = _load(connection, reference, lock=False)
        return booking


def mark_released(reference: str, actor: str) -> None:
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if booking.booking_status != "held" or booking.issuance_status not in ("not_issued", "requested"):
            raise GroupHoldRefused(409, "The booking changed meanwhile. Nothing was released.", "changed")
        connection.execute(text("UPDATE bookings SET booking_status = 'cancelled', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                           {"b": booking.id})
        _event(connection, booking.id, actor, "hold_released", {"pnr": booking.pnr})


def accept_price(reference: str, actor: str, expected_total_minor: int, total_minor: int, supplier_cost_minor: int) -> GroupBooking:
    """The customer agreed to the re-checked price: new total and supplier cost, payment status recomputed."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if booking.total_minor != expected_total_minor or booking.booking_status != "held":
            raise GroupHoldRefused(409, "The booking changed meanwhile. Check the price again.", "changed")
        status = payment_status(booking.paid_minor, total_minor)
        connection.execute(
            text("UPDATE bookings SET total_minor = :t, supplier_cost_minor = :c, payment_status = :s, updated_at = now(),"
                 " revision = revision + 1 WHERE id = :b"),
            {"t": total_minor, "c": supplier_cost_minor, "s": status, "b": booking.id},
        )
        _event(connection, booking.id, actor, "price_change_accepted",
               {"amount_minor": total_minor, "previous_amount_minor": booking.total_minor, "currency": booking.currency, "payment_status": status})
        return _load(connection, reference, lock=False)
