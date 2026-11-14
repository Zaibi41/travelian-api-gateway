"""One-time import of legacy bookings from a DynamoDB console CSV export.

LOCAL / TEST DATA ONLY unless separately approved. Reads rows with pk=BOOKING
and copies booking fields only (no travelers, passports, contacts). Every row is
tagged with --source (default "legacy_test_csv") so it can be removed:

    .venv/bin/python -m app.cli.import_bookings_csv PATH.csv            # dry run (default)
    .venv/bin/python -m app.cli.import_bookings_csv PATH.csv --apply    # write
    .venv/bin/python -m app.cli.import_bookings_csv --remove            # delete tagged rows

Output is counts only; no booking or personal values are printed.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import text

PRODUCTS = {"flight", "group", "hotel", "umrah"}
STATUSES = {"held", "pending_payment", "payment_received", "confirmed", "ticket_issued", "cancelled", "expired"}
PNR = re.compile(r"^[A-Z0-9]{1,16}$")
CURRENCY = re.compile(r"^[A-Z]{3}$")


def _dynamo_value(value):
    """Unwrap DynamoDB JSON ({"N": "1"}, {"S": "x"}) or return plain values."""
    if isinstance(value, dict) and len(value) == 1:
        (kind, inner), = value.items()
        if kind == "N":
            return inner
        if kind in {"S", "BOOL"}:
            return inner
        if kind == "NULL":
            return None
    return value


def _minor(value) -> int | None:
    try:
        amount = Decimal(str(_dynamo_value(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _timestamp(value: str) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def map_row(row: dict[str, str], source: str) -> tuple[dict | None, str | None, list[str]]:
    """Return (record, skip_reason, notes)."""
    notes: list[str] = []
    if (row.get("deletedAt") or "").strip():
        return None, "deleted", notes
    reference = (row.get("bookingId") or row.get("id") or row.get("sk", "").removeprefix("BOOKING#")).strip()
    if not reference or len(reference) > 64:
        return None, "missing or long reference", notes
    product = (row.get("type") or "flight").strip().lower()
    if product not in PRODUCTS:
        return None, f"product {product!r} not supported", notes
    status = (row.get("status") or "").strip().lower()
    if status not in STATUSES:
        return None, "unknown status", notes
    created_at = _timestamp(row.get("createdAt", ""))
    if created_at is None:
        return None, "missing createdAt", notes
    try:
        pricing = json.loads(row["pricing"]) if row.get("pricing") else {}
    except json.JSONDecodeError:
        return None, "unreadable pricing", notes
    currency = str(_dynamo_value(pricing.get("currency")) or "").strip().upper()
    total = _minor(pricing.get("total"))
    if not CURRENCY.fullmatch(currency) or total is None:
        return None, "missing price or currency", notes
    payment = (row.get("paymentStatus") or "").strip().lower()
    if payment not in {"paid", "unpaid"}:
        notes.append(f"payment {payment or 'blank'} -> unpaid")
    pnr = (row.get("pnr") or "").strip().upper() or None
    if pnr and not PNR.fullmatch(pnr):
        notes.append("pnr dropped (invalid format)")
        pnr = None
    supplier_cost = _minor(pricing.get("supplierCost")) if "supplierCost" in pricing else None
    if supplier_cost is None:
        notes.append("no supplier cost: cost = total (profit 0)")
    provider = (row.get("provider") or "").strip() or None
    if provider is None:
        notes.append("no provider")
    return {
        "id": str(uuid.uuid4()),
        "reference": reference,
        "pnr": pnr,
        "product": product,
        "provider_id": provider[:64] if provider else None,
        "booking_status": status,
        "payment_status": "paid" if payment == "paid" else "unpaid",
        "issuance_status": "issued" if (row.get("ticketStatus") or "").strip().lower() == "issued" else "not_issued",
        "hold_expires_at": _timestamp(row.get("holdExpiresAt", "")),
        "currency": currency,
        "total_minor": total,
        "supplier_cost_minor": supplier_cost if supplier_cost is not None else total,  # required since migration 0021
        "import_source": source,
        "created_at": created_at,
        "updated_at": _timestamp(row.get("updatedAt", "")) or created_at,
    }, None, notes


_INSERT = text(
    """INSERT INTO bookings (id, reference, pnr, product, provider_id, booking_status, payment_status,
           issuance_status, hold_expires_at, currency, total_minor, supplier_cost_minor, import_source,
           created_at, updated_at)
       VALUES (:id, :reference, :pnr, :product, :provider_id, :booking_status, :payment_status,
           :issuance_status, :hold_expires_at, :currency, :total_minor, :supplier_cost_minor, :import_source,
           :created_at, :updated_at)
       ON CONFLICT (reference) DO NOTHING"""
)


def _engine():
    from app.db.agency_applications import application_engine

    return application_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", nargs="?", type=Path)
    parser.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    parser.add_argument("--remove", action="store_true", help="delete rows tagged with --source")
    parser.add_argument("--source", default="legacy_test_csv")
    args = parser.parse_args(argv)

    if args.remove:
        with _engine().begin() as connection:
            removed = connection.execute(text("DELETE FROM bookings WHERE import_source = :s"), {"s": args.source}).rowcount
        print(f"Removed {removed} bookings tagged {args.source!r}.")
        return 0
    if not args.csv:
        parser.error("CSV path required (or --remove)")

    with args.csv.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("pk") == "BOOKING"]
    records, skipped, notes = [], Counter(), Counter()
    for row in rows:
        record, reason, row_notes = map_row(row, args.source)
        notes.update(row_notes)
        if record is None:
            skipped[reason] += 1
        else:
            records.append(record)

    print(f"Booking rows in CSV: {len(rows)} | importable: {len(records)} | skipped: {sum(skipped.values())}")
    for reason, count in skipped.most_common():
        print(f"  skipped  {count:3}  {reason}")
    for note, count in notes.most_common():
        print(f"  note     {count:3}  {note}")
    print("  by status  ", dict(Counter(r["booking_status"] for r in records)))
    print("  by currency", dict(Counter(r["currency"] for r in records)))

    if not args.apply:
        print("Dry run: nothing written. Re-run with --apply to import.")
        return 0
    with _engine().begin() as connection:
        inserted = sum(connection.execute(_INSERT, record).rowcount for record in records)
    print(f"Imported {inserted} bookings (already present: {len(records) - inserted}), tagged {args.source!r}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
