"""Encrypt stored identity values (gateway#84): passport / ID numbers and dates of birth.

    .venv/bin/python -m app.cli.encrypt_identity                     # dry run: counts only, changes nothing
    .venv/bin/python -m app.cli.encrypt_identity --apply             # encrypt plain rows; re-encrypt rows on an old key
    .venv/bin/python -m app.cli.encrypt_identity --decrypt           # dry run of the way back
    .venv/bin/python -m app.cli.encrypt_identity --decrypt --apply   # back to plain text (before a migration downgrade)

Needs the identity keys (PII_KEYS, PII_ACTIVE_KEY, PII_HASH_KEY in the environment or Parameter Store) for --apply.
Safe to run again: a finished row is skipped, and an interrupted run continues where it stopped (each batch is its own
transaction; rows being changed by the app at the same moment are skipped and picked up on the next run). The gateway
reads plain and encrypted rows alike, so it keeps working during the run. Prints counts only, never a value.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlalchemy import text

from app.core import pii
from app.db import agencies, identity

BATCH = 500

# table: (plain column, encrypted column, field name, is a date), and the keyed-hash columns kept with it.
TABLES = {
    "booking_travelers": [("date_of_birth", "date_of_birth_enc", identity.TRAVELER_DOB, True),
                          ("document_number", "document_number_enc", identity.TRAVELER_DOCUMENT, False)],
    "saved_people": [("date_of_birth", "date_of_birth_enc", identity.PERSON_DOB, True)],
    "saved_person_documents": [("document_number", "document_number_enc", identity.DOCUMENT_NUMBER, False)],
}


def _pending_where(table: str, decrypt: bool) -> str:
    """Rows that still need work. Encrypt: a plain value left, or a value on a key other than the active one.
    Decrypt: any encrypted value left."""
    parts = []
    for plain, encrypted, _field, _is_date in TABLES[table]:
        if decrypt:
            parts.append(f"{encrypted} IS NOT NULL")
        else:
            parts.append(f"{plain} IS NOT NULL")
            parts.append(f"({encrypted} IS NOT NULL AND split_part({encrypted}, ':', 1) <> :active)")
    if table == "saved_people" and not decrypt:
        parts.append("(date_of_birth_enc IS NOT NULL AND date_of_birth_hmac IS NULL)")
    if table == "saved_person_documents":
        parts.append("document_number_hmac IS NOT NULL" if decrypt else "document_number_normalized IS NOT NULL")
    return " OR ".join(parts)


def count(connection, decrypt: bool, active: str | None) -> dict[str, int]:
    return {table: connection.scalar(text(f"SELECT count(*) FROM {table} WHERE {_pending_where(table, decrypt)}"), {"active": active or ""})
            for table in TABLES}


def _value(row, plain: str, encrypted: str, field: str, is_date: bool, keys: pii.Keys) -> str | None:
    if row[encrypted]:
        return pii.unseal(row[encrypted], field, keys)
    value = row[plain]
    if value is None:
        return None
    return value.isoformat() if is_date else str(value).strip() or None


def _batch(connection, table: str, decrypt: bool, keys: pii.Keys) -> int:
    columns = [c for spec in TABLES[table] for c in spec[:2]]
    extra = ", id_type, document_country" if table == "saved_person_documents" else ""
    rows = connection.execute(
        text(f"SELECT id, {', '.join(columns)}{extra} FROM {table} WHERE {_pending_where(table, decrypt)}"
             f" ORDER BY id LIMIT {BATCH} FOR UPDATE SKIP LOCKED"),
        {"active": keys.active_id},
    ).mappings().all()
    for row in rows:
        changes: dict[str, object] = {}
        for plain, encrypted, field, is_date in TABLES[table]:
            value = _value(row, plain, encrypted, field, is_date, keys)
            if decrypt:
                changes[plain] = (date.fromisoformat(value) if is_date and value else value)
                changes[encrypted] = None
            else:
                changes[plain] = None
                changes[encrypted] = pii.seal(value, field, keys) if value else None
            if table == "saved_people":
                changes["date_of_birth_hmac"] = None if decrypt or not value else pii.blind_index(value, identity.PERSON_DOB, keys)
            if table == "saved_person_documents":
                normalized = pii.normalize_document(value or "", row["id_type"], row["document_country"]) or None
                changes["document_number_normalized"] = normalized if decrypt else None
                changes["document_number_hmac"] = None if decrypt else pii.blind_index(normalized, identity.DOCUMENT_KEY, keys)
        assignments = ", ".join(f"{column} = :{column}" for column in changes)
        connection.execute(text(f"UPDATE {table} SET {assignments} WHERE id = :id"), {**changes, "id": row["id"]})
    return len(rows)


def run(engine, decrypt: bool = False) -> dict[str, int]:
    """Process every pending row, a batch per transaction. Returns rows changed per table."""
    keys = pii.load_keys()
    if keys is None:
        raise pii.PiiNotConfigured("Set PII_KEYS, PII_ACTIVE_KEY and PII_HASH_KEY first.")
    done = {table: 0 for table in TABLES}
    for table in TABLES:
        while True:
            with engine.begin() as connection:
                changed = _batch(connection, table, decrypt, keys)
            done[table] += changed
            if changed < BATCH:
                break
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Encrypt (or decrypt) stored passport / ID numbers and dates of birth.")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run, counts only)")
    parser.add_argument("--decrypt", action="store_true", help="turn encrypted values back into plain text")
    args = parser.parse_args(argv)
    engine = agencies.application_engine()
    try:
        keys = pii.load_keys()
    except pii.PiiNotConfigured as error:
        print(f"Identity keys are not valid: {error}")
        return 2
    with engine.begin() as connection:
        pending = count(connection, args.decrypt, keys.active_id if keys else None)
    summary = ", ".join(f"{table}: {n}" for table, n in pending.items())
    if not args.apply:
        print(f"Dry run ({'decrypt' if args.decrypt else 'encrypt'}). Rows to change: {summary}. Nothing changed."
              + ("" if keys else " Identity keys are not set: --apply needs them."))
        return 0
    if keys is None:
        print("Identity keys are not set (PII_KEYS, PII_ACTIVE_KEY, PII_HASH_KEY). Nothing changed.")
        return 2
    done = run(engine, args.decrypt)
    with engine.begin() as connection:
        left = count(connection, args.decrypt, keys.active_id)
    print(f"Done ({'decrypt' if args.decrypt else 'encrypt'}). Rows changed: {', '.join(f'{t}: {n}' for t, n in done.items())}. "
          f"Still pending (in use during the run; run again): {', '.join(f'{t}: {n}' for t, n in left.items())}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
