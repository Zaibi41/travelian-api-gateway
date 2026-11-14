"""Group Ticketing holds in `bookings` (product 'group'), with audit entries in `booking_events`.

A group hold is created by supplier_bookings.save_hold(product="group"). Its state changes here
lock the booking row and check the current state in the same transaction:
- expire: a held booking whose hold time has passed becomes 'expired' (it is never held again).
- release: a held booking becomes 'cancelled' after the supplier confirmed the release.
- accept_price: the customer agreed to the re-checked price; the total and supplier cost change.
- release state (supplier_response -> 'release'): whether the seats were given back at the supplier
  (pending / released / failed, attempts, last error). The expiry job retries pending releases.
supplier_ref and supplier_response never leave the gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.db import agencies
from app.db.booking_payments import payment_status
from app.db.supplier_bookings import _event

RELEASE_PENDING, RELEASE_DONE, RELEASE_FAILED = "pending", "released", "failed"


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
    release: dict[str, Any]  # {} until the seats need releasing; then status, attempts, last_error, updated_at

    def __repr__(self) -> str:  # never print the supplier reference
        return f"GroupBooking({self.reference}, {self.booking_status})"

    @property
    def release_failed(self) -> bool:
        """The supplier still holds the seats after a failed release (retrying or given up)."""
        return self.release.get("status") in (RELEASE_PENDING, RELEASE_FAILED) and int(self.release.get("attempts") or 0) > 0


def _load(connection, reference: str, lock: bool) -> GroupBooking:
    row = connection.execute(
        text("SELECT id, reference, pnr, provider_id, booking_status, issuance_status, payment_status, hold_expires_at, currency,"
             " total_minor, supplier_cost_minor, supplier_ref, itinerary, supplier_response -> 'adults' AS adults,"
             " supplier_response -> 'release' AS release"
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
                        row["supplier_ref"], row["itinerary"] if isinstance(row["itinerary"], list) else [], int(adults or 0), int(paid),
                        row["release"] if isinstance(row["release"], dict) else {})


def load(reference: str) -> GroupBooking:
    with agencies.application_engine().begin() as connection:
        return _load(connection, reference, lock=False)


def is_over(booking: GroupBooking, now: datetime) -> bool:
    return booking.hold_expires_at is not None and booking.hold_expires_at <= now


def _set_release(connection, booking_id: str, state: dict[str, Any]) -> None:
    connection.execute(
        text("UPDATE bookings SET supplier_response = COALESCE(supplier_response, '{}'::jsonb) || jsonb_build_object('release', CAST(:s AS jsonb)),"
             " updated_at = now(), revision = revision + 1 WHERE id = :b"),
        {"s": json.dumps(state, default=str), "b": booking_id},
    )


def _expire(connection, booking: GroupBooking, now: datetime) -> None:
    """held -> expired, seats to be released at the supplier by the expiry job."""
    connection.execute(text("UPDATE bookings SET booking_status = 'expired', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                       {"b": booking.id})
    _set_release(connection, booking.id, {"status": RELEASE_PENDING, "attempts": 0, "updated_at": now.isoformat()})
    _event(connection, booking.id, None, "hold_expired", {"pnr": booking.pnr, "time_limit": booking.hold_expires_at.isoformat() if booking.hold_expires_at else None})


def _expirable(booking: GroupBooking, now: datetime) -> bool:
    return booking.booking_status == "held" and is_over(booking, now) and booking.issuance_status in ("not_issued", "requested")


def expire_if_over(reference: str, now: datetime) -> GroupBooking:
    """Mark a held booking whose time limit has passed as expired (once, with an audit entry).
    Leaves a booking that is being issued, or whose issuing is unclear, alone."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if _expirable(booking, now):
            _expire(connection, booking, now)
            booking = _load(connection, reference, lock=False)
        return booking


def expire_due(now: datetime) -> list[str]:
    """Every held group booking past its time limit becomes expired. Returns their references."""
    with agencies.application_engine().begin() as connection:
        references = connection.execute(
            text("SELECT reference FROM bookings WHERE product = 'group' AND booking_status = 'held' AND hold_expires_at <= :now"
                 " AND issuance_status IN ('not_issued', 'requested') ORDER BY hold_expires_at"), {"now": now},
        ).scalars().all()
    expired = []
    for reference in references:
        with agencies.application_engine().begin() as connection:
            booking = _load(connection, reference, lock=True)
            if _expirable(booking, now):
                _expire(connection, booking, now)
                expired.append(reference)
    return expired


def releases_due(max_attempts: int) -> list[GroupBooking]:
    """Bookings whose seats still need releasing at the supplier (pending, under the attempt limit)."""
    with agencies.application_engine().begin() as connection:
        references = connection.execute(
            text("SELECT reference FROM bookings WHERE product = 'group' AND supplier_response -> 'release' ->> 'status' = :pending"
                 " AND COALESCE((supplier_response -> 'release' ->> 'attempts')::int, 0) < :max ORDER BY updated_at"),
            {"pending": RELEASE_PENDING, "max": max_attempts},
        ).scalars().all()
        return [_load(connection, reference, lock=False) for reference in references]


def mark_release_attempt(reference: str, actor: str | None, ok: bool, error: str | None, now: datetime, max_attempts: int) -> GroupBooking:
    """Record one supplier release call: released, or another failed attempt (failed once the limit is reached)."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        attempts = int(booking.release.get("attempts") or 0) + 1
        if ok:
            _set_release(connection, booking.id, {"status": RELEASE_DONE, "attempts": attempts, "updated_at": now.isoformat()})
            _event(connection, booking.id, actor, "supplier_released", {"pnr": booking.pnr})
        else:
            status = RELEASE_FAILED if attempts >= max_attempts else RELEASE_PENDING
            _set_release(connection, booking.id, {"status": status, "attempts": attempts, "last_error": error, "updated_at": now.isoformat()})
            _event(connection, booking.id, actor, "supplier_release_failed", {"pnr": booking.pnr, "supplier_error": error, "reason_code": status})
        return _load(connection, reference, lock=False)


def mark_released(reference: str, actor: str, now: datetime) -> None:
    """Staff released the hold and the supplier confirmed: the booking is cancelled (released)."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if booking.booking_status != "held" or booking.issuance_status not in ("not_issued", "requested"):
            raise GroupHoldRefused(409, "The booking changed meanwhile. Nothing was released.", "changed")
        connection.execute(text("UPDATE bookings SET booking_status = 'cancelled', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                           {"b": booking.id})
        _set_release(connection, booking.id, {"status": RELEASE_DONE, "attempts": 1, "updated_at": now.isoformat()})
        _event(connection, booking.id, actor, "hold_released", {"pnr": booking.pnr})


def mark_release_failed_on_request(reference: str, actor: str, error: str | None, now: datetime) -> None:
    """Staff released the hold but the supplier didn't confirm: the booking reads as expired and the
    expiry job keeps trying to release the seats."""
    with agencies.application_engine().begin() as connection:
        booking = _load(connection, reference, lock=True)
        if booking.booking_status != "held":
            raise GroupHoldRefused(409, "The booking changed meanwhile.", "changed")
        connection.execute(text("UPDATE bookings SET booking_status = 'expired', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                           {"b": booking.id})
        _set_release(connection, booking.id, {"status": RELEASE_PENDING, "attempts": 1, "last_error": error, "updated_at": now.isoformat()})
        _event(connection, booking.id, actor, "hold_released", {"pnr": booking.pnr, "supplier_error": error, "reason_code": "supplier_release_failed"})


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
