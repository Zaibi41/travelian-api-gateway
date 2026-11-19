"""Paid website flights that weren't ticketed (D48): the stuck-issuance recovery and the 'paid_not_ticketed' state.

Every change here locks the booking row and re-checks its state in the same transaction, so two workers (or a worker
and a staff click) never both act on one booking:
- claim_recovery writes the booking's single 'auto_issue_recovery' entry. A booking gets at most one, ever (the
  automatic retry happens once); a second claim finds it and refuses.
- mark_paid_not_ticketed moves a paid, not-issued booking to 'paid_not_ticketed' (refund or rebook by hand).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.db import agencies
from app.db.supplier_bookings import _event

OPEN = ("held", "pending_payment", "payment_received")
RECOVERY_EVENT = "auto_issue_recovery"

# Fully paid by a verified PayFast payment (card), not issued or stuck mid-issue, untouched since `stuck_before`, and
# never retried. Bank transfer and JazzCash payments (recorded by staff) never match: staff issue those (D49).
_STUCK = (
    " FROM bookings b WHERE b.product = 'flight' AND b.booking_status IN ('held', 'pending_payment', 'payment_received')"
    " AND b.issuance_status IN ('not_issued', 'processing') AND b.updated_at <= :before"
    " AND (SELECT COALESCE(SUM(p.amount_minor), 0) FROM booking_payments p WHERE p.booking_id = b.id) >= b.total_minor"
    " AND EXISTS (SELECT 1 FROM payfast_attempts a WHERE a.booking_id = b.id AND a.status = 'paid')"
    " AND NOT EXISTS (SELECT 1 FROM booking_events e WHERE e.booking_id = b.id AND e.action = 'auto_issue_recovery')"
)


def stuck_card_payments(before: datetime) -> list[tuple[str, str, str | None]]:
    """(reference, issuance status, provider) of each candidate, oldest first. Read-only."""
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text("SELECT b.reference, b.issuance_status, b.provider_id" + _STUCK + " ORDER BY b.updated_at"),
                                  {"before": before}).all()
    return [(row.reference, row.issuance_status, row.provider_id) for row in rows]


def claim_recovery(reference: str, before: datetime, details: dict[str, Any]) -> str | None:
    """Take this booking's one recovery, if it still qualifies: returns its issuance status then, else None (another worker
    has it, it changed meanwhile, or it was already retried). A locked row is skipped, never waited for."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("SELECT b.id, b.issuance_status" + _STUCK + " AND b.reference = :r FOR UPDATE OF b SKIP LOCKED"),
                                 {"before": before, "r": reference}).one_or_none()
        if row is None:
            return None
        _event(connection, row.id, None, RECOVERY_EVENT, {**details, "issuance_status": row.issuance_status})
    return row.issuance_status


def mark_paid_not_ticketed(reference: str, reason: str, now: datetime, details: dict[str, Any] | None = None) -> bool:
    """A paid flight that can't be ticketed -> 'paid_not_ticketed', with the reason. Only from an open booking with money
    recorded and no ticket (issuance not started, requested or not issued). False when it no longer qualifies."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "SELECT id, booking_status, issuance_status, (SELECT COALESCE(SUM(p.amount_minor), 0) FROM booking_payments p"
            " WHERE p.booking_id = b.id) AS paid FROM bookings b WHERE reference = :r AND product = 'flight' FOR UPDATE"),
            {"r": reference}).one_or_none()
        if row is None or row.booking_status not in OPEN or row.issuance_status not in ("not_issued", "requested") or not row.paid:
            return False
        action = {"reason": reason, "at": now.isoformat(), **(details or {})}
        connection.execute(text(
            "UPDATE bookings SET booking_status = 'paid_not_ticketed', supplier_response = COALESCE(supplier_response, '{}'::jsonb)"
            " || jsonb_build_object('action_needed', CAST(:a AS jsonb)), updated_at = now(), revision = revision + 1 WHERE id = :b"),
            {"a": json.dumps(action, default=str), "b": row.id})
        _event(connection, row.id, None, "paid_not_ticketed", {**action, "amount_minor": int(row.paid)})
    return True
