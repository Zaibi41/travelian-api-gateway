"""Issuance state for a booking (migration 0014). Every change locks the booking row and checks
the current issuance status in the same transaction, so two clicks can never both start a
ticketing call: the first moves the booking to 'processing', the second is refused.

Every step writes a booking_events entry. Ticket numbers are stored per traveler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.db import agencies
from app.db.supplier_bookings import _event

#: Statuses from which an approver may start issuing.
ISSUABLE = ("not_issued", "requested")


class IssuanceRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Target:
    id: str
    reference: str
    pnr: str | None
    provider: str | None
    product: str
    booking_status: str
    issuance_status: str
    hold_expires_at: datetime | None
    currency: str
    total_minor: int
    supplier_cost_minor: int | None
    supplier_ref: str | None
    paid_minor: int
    traveler_types: list[tuple[int, str]]  # (position, passenger type) in booking order


def _load(connection, reference: str, lock: bool) -> Target:
    row = connection.execute(
        text("SELECT id, reference, pnr, provider_id, product, booking_status, issuance_status, hold_expires_at, currency, total_minor,"
             " supplier_cost_minor, supplier_ref FROM bookings WHERE reference = :r" + (" FOR UPDATE" if lock else "")),
        {"r": reference},
    ).mappings().one_or_none()
    if row is None:
        raise IssuanceRefused(404, "Booking not found.")
    paid = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments WHERE booking_id = :b"), {"b": row["id"]})
    travelers = connection.execute(
        text("SELECT position, passenger_type FROM booking_travelers WHERE booking_id = :b ORDER BY position"), {"b": row["id"]},
    ).all()
    return Target(row["id"], row["reference"], row["pnr"], row["provider_id"], row["product"], row["booking_status"], row["issuance_status"],
                  row["hold_expires_at"], row["currency"], row["total_minor"], row["supplier_cost_minor"], row["supplier_ref"], paid,
                  [(t.position, t.passenger_type) for t in travelers])


def load(reference: str) -> Target:
    with agencies.application_engine().begin() as connection:
        return _load(connection, reference, lock=False)


def blockers(target: Target, now: datetime, margin_seconds: int) -> list[str]:
    """Why this booking can't be issued now (empty when it can). Same rules for request and issue."""
    reasons = []
    if target.product != "flight" or not target.pnr or not target.supplier_ref:
        reasons.append("This booking has no supplier hold to issue.")
    if target.booking_status != "held":
        reasons.append("The booking isn't on hold.")
    if target.hold_expires_at is None or (target.hold_expires_at - now).total_seconds() <= margin_seconds:
        reasons.append("The hold has expired or is about to expire.")
    if target.paid_minor < target.total_minor:
        reasons.append("Full payment isn't recorded yet.")
    return reasons


def _refuse_by_status(status: str) -> None:
    if status == "processing":
        raise IssuanceRefused(409, "Issuing is already in progress for this booking.")
    if status == "issued":
        raise IssuanceRefused(409, "This booking is already issued.")
    if status == "unknown":
        raise IssuanceRefused(409, "The issuance status is unknown. Verify it with the supplier or resolve it before issuing again.")


def request(reference: str, actor: str, now: datetime, margin_seconds: int, note: str | None) -> Target:
    with agencies.application_engine().begin() as connection:
        target = _load(connection, reference, lock=True)
        _refuse_by_status(target.issuance_status)
        if target.issuance_status == "requested":
            raise IssuanceRefused(409, "Issuance is already requested for this booking.")
        reasons = blockers(target, now, margin_seconds)
        if reasons:
            raise IssuanceRefused(409, " ".join(reasons))
        _set(connection, target.id, "requested")
        _event(connection, target.id, actor, "issuance_requested", {"pnr": target.pnr, **({"note": note} if note else {})})
    return target


def claim(reference: str, actor: str, now: datetime, margin_seconds: int) -> Target:
    """Move to 'processing' (the only way a ticketing call may start). Returns the target as it was."""
    with agencies.application_engine().begin() as connection:
        target = _load(connection, reference, lock=True)
        _refuse_by_status(target.issuance_status)
        reasons = blockers(target, now, margin_seconds)
        if reasons:
            raise IssuanceRefused(409, " ".join(reasons))
        _set(connection, target.id, "processing")
        _event(connection, target.id, actor, "issuance_started", {"pnr": target.pnr, "previous_status": target.issuance_status})
    return target


def _set(connection, booking_id: str, status: str, extra: str = "", params: dict[str, Any] | None = None) -> None:
    connection.execute(
        text(f"UPDATE bookings SET issuance_status = :s{extra}, updated_at = now(), revision = revision + 1 WHERE id = :b"),
        {"s": status, "b": booking_id, **(params or {})},
    )


def _store_response(response: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    if response is None:
        return "", {}
    return (", supplier_response = COALESCE(supplier_response, '{}'::jsonb) || jsonb_build_object('ticketing', CAST(:response AS jsonb))",
            {"response": json.dumps(response, default=str)})


def release(target: Target, actor: str, status: str, action: str, details: dict[str, Any], response: dict[str, Any] | None = None) -> None:
    """Leave 'processing' without a ticket: back to a retryable status (supplier confirmed nothing
    was issued, or nothing was sent) or 'unknown'."""
    with agencies.application_engine().begin() as connection:
        current = connection.scalar(text("SELECT issuance_status FROM bookings WHERE id = :b FOR UPDATE"), {"b": target.id})
        if current != "processing":
            raise IssuanceRefused(409, "The issuance changed meanwhile.")
        extra, params = _store_response(response)
        _set(connection, target.id, status, extra, params)
        _event(connection, target.id, actor, action, details)


def mark_issued(target: Target, actor: str, tickets: dict[int, str], action: str, details: dict[str, Any],
                response: dict[str, Any] | None = None, expected: tuple[str, ...] = ("processing",)) -> None:
    with agencies.application_engine().begin() as connection:
        current = connection.scalar(text("SELECT issuance_status FROM bookings WHERE id = :b FOR UPDATE"), {"b": target.id})
        if current not in expected:
            raise IssuanceRefused(409, "The issuance changed meanwhile.")
        extra, params = _store_response(response)
        _set(connection, target.id, "issued", ", booking_status = 'ticket_issued'" + extra, params)
        for position, number in tickets.items():
            connection.execute(text("UPDATE booking_travelers SET ticket_number = :t WHERE booking_id = :b AND position = :p"),
                               {"t": number, "b": target.id, "p": position})
        _event(connection, target.id, actor, action, {**details, "tickets": len(tickets)})


def resolve_not_issued(target: Target, actor: str, action: str, details: dict[str, Any], expected: tuple[str, ...] = ("unknown",)) -> None:
    with agencies.application_engine().begin() as connection:
        current = connection.scalar(text("SELECT issuance_status FROM bookings WHERE id = :b FOR UPDATE"), {"b": target.id})
        if current not in expected:
            raise IssuanceRefused(409, "The issuance changed meanwhile.")
        _set(connection, target.id, "not_issued")
        _event(connection, target.id, actor, action, details)
