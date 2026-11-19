"""Flight hold expiry (run by the hold expiry job, app/services/group_hold_expiry.py).

A flight booking whose hold time has passed and that isn't issued becomes 'expired'. The hold lapses at the airline by
itself, so nothing is sent to any supplier. Covered: held, pending payment and payment received, with issuance not
started or only requested. Left alone: issued, cancelled, already expired, and issuing in progress or unclear
(processing, unknown: a ticket may exist; staff resolve those). A booking with money recorded becomes
'paid_not_ticketed' instead (the customer paid and has no ticket: staff refund or rebook by hand, and are alerted); the
payment stays as it is. Each expiry is logged on the booking (hold_expired).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app.db import agencies
from app.db.supplier_bookings import _event

EXPIRABLE_STATUSES = ("held", "pending_payment", "payment_received")
EXPIRABLE_ISSUANCE = ("not_issued", "requested")

_DUE = (" FROM bookings b WHERE b.product = 'flight' AND b.booking_status IN ('held', 'pending_payment', 'payment_received')"
        " AND b.issuance_status IN ('not_issued', 'requested') AND b.hold_expires_at IS NOT NULL AND b.hold_expires_at <= :now")


@dataclass(frozen=True)
class FlightExpired:
    reference: str
    paid_minor: int
    currency: str
    status: str = "expired"  # or 'paid_not_ticketed' when money was recorded
    pnr: str | None = None
    hold_expires_at: datetime | None = None
    total_minor: int | None = None


def count_due(now: datetime) -> int:
    """How many flight bookings a run at `now` would expire (read-only)."""
    with agencies.application_engine().begin() as connection:
        return int(connection.scalar(text("SELECT count(*)" + _DUE), {"now": now}) or 0)


def expire_due(now: datetime) -> list[FlightExpired]:
    """Expire every flight booking due at `now`. Rows locked by another transaction are skipped (next run)."""
    expired: list[FlightExpired] = []
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(
            text("SELECT b.id, b.reference, b.pnr, b.currency, b.hold_expires_at, b.total_minor,"
                 " (SELECT COALESCE(SUM(p.amount_minor), 0) FROM booking_payments p WHERE p.booking_id = b.id) AS paid" + _DUE
                 + " ORDER BY b.hold_expires_at FOR UPDATE OF b SKIP LOCKED"), {"now": now},
        ).mappings().all()
        for row in rows:
            status = "paid_not_ticketed" if row["paid"] else "expired"
            action = json.dumps({"reason": "hold_expired", "at": now.isoformat(), "amount_minor": int(row["paid"])}) if row["paid"] else None
            connection.execute(text(
                "UPDATE bookings SET booking_status = :s, supplier_response = CASE WHEN CAST(:a AS jsonb) IS NULL THEN supplier_response"
                " ELSE COALESCE(supplier_response, '{}'::jsonb) || jsonb_build_object('action_needed', CAST(:a AS jsonb)) END,"
                " updated_at = now(), revision = revision + 1 WHERE id = :b"), {"s": status, "a": action, "b": row["id"]})
            details = {"pnr": row["pnr"], "time_limit": row["hold_expires_at"].isoformat()}
            if row["paid"]:
                details.update(amount_minor=int(row["paid"]), currency=row["currency"], reason_code="payment_recorded_refund_by_hand",
                               status=status)
            _event(connection, row["id"], None, "hold_expired", details)
            expired.append(FlightExpired(row["reference"], int(row["paid"]), row["currency"], status, row["pnr"], row["hold_expires_at"],
                                         int(row["total_minor"])))
    return expired
