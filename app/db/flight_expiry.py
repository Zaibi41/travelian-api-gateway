"""Flight hold expiry (run by the hold expiry job, app/services/group_hold_expiry.py).

A flight booking whose hold time has passed and that isn't issued becomes 'expired'. The hold lapses at the airline by
itself, so nothing is sent to any supplier. Covered: held, pending payment and payment received, with issuance not
started or only requested. Left alone: issued, cancelled, already expired, and issuing in progress or unclear
(processing, unknown: a ticket may exist; staff resolve those). A payment already recorded stays as it is; the job
reports the booking so the refund can be handled by hand. Each expiry is logged on the booking (hold_expired).
"""

from __future__ import annotations

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


def count_due(now: datetime) -> int:
    """How many flight bookings a run at `now` would expire (read-only)."""
    with agencies.application_engine().begin() as connection:
        return int(connection.scalar(text("SELECT count(*)" + _DUE), {"now": now}) or 0)


def expire_due(now: datetime) -> list[FlightExpired]:
    """Expire every flight booking due at `now`. Rows locked by another transaction are skipped (next run)."""
    expired: list[FlightExpired] = []
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(
            text("SELECT b.id, b.reference, b.pnr, b.currency, b.hold_expires_at,"
                 " (SELECT COALESCE(SUM(p.amount_minor), 0) FROM booking_payments p WHERE p.booking_id = b.id) AS paid" + _DUE
                 + " ORDER BY b.hold_expires_at FOR UPDATE OF b SKIP LOCKED"), {"now": now},
        ).mappings().all()
        for row in rows:
            connection.execute(text("UPDATE bookings SET booking_status = 'expired', updated_at = now(), revision = revision + 1 WHERE id = :b"),
                               {"b": row["id"]})
            details = {"pnr": row["pnr"], "time_limit": row["hold_expires_at"].isoformat()}
            if row["paid"]:
                details.update(amount_minor=int(row["paid"]), currency=row["currency"], reason_code="payment_recorded_refund_by_hand")
            _event(connection, row["id"], None, "hold_expired", details)
            expired.append(FlightExpired(row["reference"], int(row["paid"]), row["currency"]))
    return expired
