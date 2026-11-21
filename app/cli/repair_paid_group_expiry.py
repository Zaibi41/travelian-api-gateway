"""One-off repair (gateway#109): group bookings the expiry job marked 'expired' although money was recorded on them.

    .venv/bin/python -m app.cli.repair_paid_group_expiry            # dry run: lists what would change, changes nothing
    .venv/bin/python -m app.cli.repair_paid_group_expiry --apply    # make the changes (one transaction per booking, logged)

Before the fix the expiry job expired a paid group hold at its time limit even when it was only waiting for the final
PNR. Only bookings it expired are looked at: product 'group', status 'expired', money recorded, no final PNR, ticket not
issued or being issued, a 'hold_expired' entry and no staff release ('hold_released'). Each becomes:
- 'held' again (waiting for the final PNR, shown as "Paid – awaiting final PNR"): paid in full, the seats were not given
  back at the supplier (release not 'released') and the supplier didn't cancel it. The pending release is dropped, so
  the expiry job doesn't give the seats back; the release state it had is kept in the log entry.
- 'paid_not_ticketed' (staff refund or rebook) otherwise: part-paid, seats released, or cancelled at the supplier.
Each change is logged on the booking ('hold_expiry_corrected', with the previous status and release state). Running it
again changes nothing (the bookings are no longer 'expired'). Prints booking references and counts only, no personal
data. Run the dry run first and check the list with the team.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.db import agencies
from app.db.group_holds import PAID_NOT_TICKETED, RELEASE_DONE
from app.db.supplier_bookings import _event

CANDIDATES = """
SELECT b.id, b.reference, b.total_minor, b.currency,
       (SELECT COALESCE(SUM(p.amount_minor), 0) FROM booking_payments p WHERE p.booking_id = b.id) AS paid,
       b.supplier_response -> 'release' AS release,
       b.supplier_response -> 'supplier_amounts' ->> 'cancel_status' AS cancel_status,
       EXISTS (SELECT 1 FROM booking_events e WHERE e.booking_id = b.id AND e.action = 'supplier_cancelled') AS supplier_cancelled
  FROM bookings b
 WHERE b.product = 'group' AND b.booking_status = 'expired' AND b.issuance_status IN ('not_issued', 'requested')
   AND COALESCE(b.supplier_response ->> 'final_pnr', '') = ''
   AND EXISTS (SELECT 1 FROM booking_events e WHERE e.booking_id = b.id AND e.action = 'hold_expired')
   AND NOT EXISTS (SELECT 1 FROM booking_events e WHERE e.booking_id = b.id AND e.action = 'hold_released')
   AND (SELECT COALESCE(SUM(p.amount_minor), 0) FROM booking_payments p WHERE p.booking_id = b.id) > 0
"""


@dataclass(frozen=True)
class Correction:
    booking_id: str
    reference: str
    to: str  # 'held' or 'paid_not_ticketed'
    paid_minor: int
    total_minor: int
    currency: str
    release: dict[str, Any]


def decide(paid_minor: int, total_minor: int, release: dict[str, Any] | None, supplier_cancelled: bool) -> str:
    """'held' (waiting for the final PNR) when paid in full with the seats still held at the supplier; else paid_not_ticketed."""
    seats_given_back = (release or {}).get("status") == RELEASE_DONE
    if paid_minor >= total_minor and not seats_given_back and not supplier_cancelled:
        return "held"
    return PAID_NOT_TICKETED


def _plan(connection, lock: bool = False) -> list[Correction]:
    rows = connection.execute(text(CANDIDATES + " ORDER BY b.created_at" + (" FOR UPDATE OF b" if lock else ""))).mappings().all()
    plan = []
    for row in rows:
        release = row["release"] if isinstance(row["release"], dict) else {}
        cancelled = bool(row["supplier_cancelled"]) or str(row["cancel_status"] or "").strip() == "1"
        plan.append(Correction(row["id"], row["reference"], decide(int(row["paid"]), int(row["total_minor"]), release, cancelled),
                               int(row["paid"]), int(row["total_minor"]), row["currency"], release))
    return plan


def plan() -> list[Correction]:
    """What --apply would change. Read-only."""
    with agencies.application_engine().begin() as connection:
        return _plan(connection)


def apply() -> list[Correction]:
    """Make the changes, each logged on its booking. Only bookings still matching at the time are changed."""
    done = []
    with agencies.application_engine().begin() as connection:
        for item in _plan(connection, lock=True):
            if item.to == "held":
                connection.execute(text("UPDATE bookings SET booking_status = 'held', supplier_response = supplier_response - 'release',"
                                        " updated_at = now(), revision = revision + 1 WHERE id = :b AND booking_status = 'expired'"),
                                   {"b": item.booking_id})
            else:
                connection.execute(text("UPDATE bookings SET booking_status = :s, updated_at = now(), revision = revision + 1"
                                        " WHERE id = :b AND booking_status = 'expired'"), {"s": PAID_NOT_TICKETED, "b": item.booking_id})
            _event(connection, item.booking_id, None, "hold_expiry_corrected",
                   {"previous_status": "expired", "status": item.to, "amount_minor": item.paid_minor, "currency": item.currency,
                    "total_minor": item.total_minor, "release": json.loads(json.dumps(item.release, default=str)),
                    "reason_code": "paid_group_hold_expired"})
            done.append(item)
    return done


def _print(items: list[Correction], applied: bool) -> None:
    verb = "changed" if applied else "would change"
    for item in items:
        print(f"{item.reference}: expired -> {item.to} (received {item.paid_minor} of {item.total_minor} {item.currency} in minor units)")
    held = sum(item.to == "held" for item in items)
    print(f"{len(items)} booking(s) {verb}: {held} back to held (waiting for the final PNR), "
          f"{len(items) - held} to paid_not_ticketed (refund or rebook).")
    if not applied and items:
        print("Dry run: nothing was changed. Run again with --apply to make these changes.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Correct paid group bookings that the expiry job marked expired (gateway#109).")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run, lists only)")
    args = parser.parse_args(argv)
    if args.apply:
        _print(apply(), applied=True)
    else:
        _print(plan(), applied=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
