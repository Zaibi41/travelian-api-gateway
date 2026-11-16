"""Helpers for traveler identity data (passport / ID number, phone, email, document expiry).

Sandbox phase: these values are stored as plain text (docs/pii-storage.md) and returned in full
to the back office; nothing is masked. Values are never logged. Protecting them in the database is a go-live blocker (docs/roadmap.md
in the frontend repo).
"""

from __future__ import annotations

import re
from datetime import date


def normalize_document(document_number: str, id_type: str | None = None, country: str | None = None) -> str:
    """The form document numbers are matched in: a Pakistani national ID (CNIC / B-form) keeps its
    digits only (dashes and spaces dropped); anything else is upper-case letters and digits."""
    if id_type == "NATI" and (country or "").upper() == "PAK":
        return re.sub(r"\D", "", document_number)
    return re.sub(r"[^A-Z0-9]", "", document_number.upper())


def normalize_email(email: str | None) -> str | None:
    value = (email or "").strip().lower()
    return value or None


def normalize_phone(phone_code: str | None, phone: str | None) -> str | None:
    """Country code and number as digits only (e.g. 92 + 3001234567 -> 923001234567)."""
    digits = re.sub(r"\D", "", f"{phone_code or ''}{(phone or '').lstrip('0')}")
    return digits or None


def document_validity(expiry: date | None, today: date) -> str | None:
    """expired (before today), expiring (within 6 months, the spec's Documents expiring rule) or valid."""
    if expiry is None:
        return None
    if expiry < today:
        return "expired"
    month = today.month - 1 + 6
    year, month = today.year + month // 12, month % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            limit = today.replace(year=year, month=month, day=min(today.day, last))
            break
        except ValueError:
            continue
    return "expiring" if expiry < limit else "valid"
