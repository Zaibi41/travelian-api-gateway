"""Where identity values live in the database (gateway#84) and how they are written and read.

With encryption on (pii keys configured) a value goes to its `*_enc` column and its plain column is left empty; the
match keys (document number, date of birth) go to `*_hmac`. With encryption off everything stays plain, as before.
Reads accept both, so rows written before the keys were set (or before the backfill) keep working.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core import pii

TRAVELER_DOB = "booking_travelers.date_of_birth"
TRAVELER_DOCUMENT = "booking_travelers.document_number"
PERSON_DOB = "saved_people.date_of_birth"
DOCUMENT_NUMBER = "saved_person_documents.document_number"
DOCUMENT_KEY = "saved_person_documents.document_number_normalized"


def text_columns(value: str | None, field: str) -> tuple[str | None, str | None]:
    """(plain, encrypted) to store for a text value."""
    value = (value or "").strip() or None
    if value is None or not pii.encryption_on():
        return value, None
    return None, pii.seal(value, field)


def date_columns(value: date | None, field: str) -> tuple[date | None, str | None]:
    """(plain, encrypted) to store for a date."""
    if value is None or not pii.encryption_on():
        return value, None
    return None, pii.seal(value.isoformat(), field)


def read_text(plain: str | None, encrypted: str | None, field: str) -> str | None:
    return pii.unseal(encrypted, field) if encrypted else plain


def read_date(plain: date | None, encrypted: str | None, field: str) -> date | None:
    return date.fromisoformat(pii.unseal(encrypted, field)) if encrypted else plain  # type: ignore[arg-type]


def date_hmac(value: date | None) -> str | None:
    """The keyed hash a saved person's date of birth is matched on (None when encryption is off)."""
    return pii.blind_index(value.isoformat(), PERSON_DOB) if value else None


def document_key(normalized: str | None) -> tuple[str | None, str | None]:
    """(plain normalised number, keyed hash) to store as a document's match key."""
    if not normalized or not pii.encryption_on():
        return normalized or None, None
    return None, pii.blind_index(normalized, DOCUMENT_KEY)


def document_hmac(normalized: str | None) -> str | None:
    """The keyed hash to look a document up by (None when encryption is off)."""
    return pii.blind_index(normalized, DOCUMENT_KEY) if normalized else None


def open_traveler(row: dict[str, Any]) -> dict[str, Any]:
    """A booking_travelers row with its date of birth and document number in plain form (enc columns dropped)."""
    out = {k: v for k, v in row.items() if not k.endswith("_enc")}
    if "date_of_birth" in row:
        out["date_of_birth"] = read_date(row["date_of_birth"], row.get("date_of_birth_enc"), TRAVELER_DOB)
    if "document_number" in row:
        out["document_number"] = read_text(row["document_number"], row.get("document_number_enc"), TRAVELER_DOCUMENT)
    return out
