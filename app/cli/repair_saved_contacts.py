"""One-off repair: saved customers that lost their email or phone at the hold (fixed in the hold save).

    .venv/bin/python -m app.cli.repair_saved_contacts            # dry run: counts only, changes nothing
    .venv/bin/python -m app.cli.repair_saved_contacts --apply    # fill them in (one transaction, logged)

Two cases, each filled from the person's earliest booking that has the values:
- a saved customer (linked to a booking's customer row) whose email or phone is empty;
- a booking's customer row left unlinked because the save stopped at a possible duplicate, while the same
  booking has a traveler with the same name saved as a person: the customer row is linked to that person.
Only empty values are filled; a stored value is never replaced. Prints counts only, no personal data.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from app.db import agencies
from app.db.saved_people import _apply, _event, _same_name, load

# Saved customers with an empty email or phone, and the earliest booking customer row that has them.
LINKED = """
SELECT DISTINCT ON (p.id) p.id AS person_id, b.agency_id AS owner, b.reference, bc.booking_id, bc.email, bc.phone_code, bc.phone
  FROM saved_people p JOIN booking_customers bc ON bc.person_id = p.id JOIN bookings b ON b.id = bc.booking_id
 WHERE p.merged_into_id IS NULL AND (coalesce(p.email, '') = '' OR coalesce(p.phone, '') = '')
   AND (coalesce(bc.email, '') <> '' OR coalesce(bc.phone, '') <> '')
 ORDER BY p.id, b.created_at
"""

# Unlinked booking customer rows whose booking has a saved traveler with the same name.
UNLINKED = """
SELECT bc.booking_id, b.agency_id AS owner, b.reference, bc.first_name, bc.last_name, bc.email, bc.phone_code, bc.phone,
       bt.person_id, p.first_name AS person_first, p.last_name AS person_last
  FROM booking_customers bc JOIN bookings b ON b.id = bc.booking_id
  JOIN booking_travelers bt ON bt.booking_id = bc.booking_id AND bt.person_id IS NOT NULL
  JOIN saved_people p ON p.id = bt.person_id AND p.merged_into_id IS NULL
 WHERE bc.person_id IS NULL AND (coalesce(bc.email, '') <> '' OR coalesce(bc.phone, '') <> '')
 ORDER BY b.created_at
"""


def plan(connection) -> tuple[list[dict], list[dict]]:
    """(saved customers to fill, customer rows to link and fill). Read-only."""
    linked = [dict(row) for row in connection.execute(text(LINKED)).mappings()]
    unlinked, seen = [], set()
    for row in connection.execute(text(UNLINKED)).mappings():
        if row["booking_id"] in seen or not (_same_name(row["first_name"], row["person_first"]) and _same_name(row["last_name"], row["person_last"])):
            continue
        seen.add(row["booking_id"])
        unlinked.append(dict(row))
    return linked, unlinked


def _missing(connection, row: dict, lock: bool = False) -> dict:
    """The booking's values the saved person lacks (a stored value is never replaced)."""
    person = load(connection, row["owner"], row["person_id"], lock=lock)
    values = {"email": row["email"], "phone_code": row["phone_code"], "phone": row["phone"]}
    missing = {k: v for k, v in values.items() if v and not getattr(person, k)}
    if missing.keys() & {"phone_code", "phone"}:
        missing |= {k: values[k] for k in ("phone_code", "phone") if values[k]}  # a phone is its code and number together
    return missing


def _fill(connection, row: dict, actor_note: str) -> list[str]:
    missing = _missing(connection, row, lock=True)
    person = load(connection, row["owner"], row["person_id"])
    _apply(connection, person, missing)
    _event(connection, person.id, None, "contact_repaired", {"booking_id": row["reference"], "fields_updated": sorted(missing), "source": actor_note})
    return sorted(missing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fill saved customers' missing email and phone from their bookings.")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run, counts only)")
    args = parser.parse_args(argv)
    engine = agencies.application_engine()
    if not args.apply:
        with engine.begin() as connection:  # reads only; nothing is written in a dry run
            linked, unlinked = plan(connection)
            gains = sum(bool(_missing(connection, row)) for row in linked + unlinked)
        print(f"Dry run. Saved customers to fill: {len(linked)}. Booking customers to link to their saved traveler: {len(unlinked)}. "
              f"Saved people that would gain an email or phone: {gains}. Nothing changed.")
        return 0
    with engine.begin() as connection:
        linked, unlinked = plan(connection)
        filled = sum(bool(_fill(connection, row, "saved_customer")) for row in linked)
        for row in unlinked:
            connection.execute(text("UPDATE booking_customers SET person_id = :p WHERE booking_id = :b AND person_id IS NULL"),
                               {"p": row["person_id"], "b": row["booking_id"]})
            _fill(connection, row, "customer_was_traveler")
    print(f"Done. Saved customers filled: {filled}. Booking customers linked and filled: {len(unlinked)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
