"""Masking for traveler identity data (date of birth, passport / ID number, document expiry).

Sandbox phase: these values are stored as plain text (docs/pii-storage.md). Every read the
gateway returns is masked here; the full values come only from the logged reveal endpoint.
Values are never logged. Protecting them in the database is a go-live blocker (docs/roadmap.md
in the frontend repo).
"""

from __future__ import annotations

import re
from datetime import date


def normalize_document(document_number: str) -> str:
    """Upper case, letters and digits only: the form to compare document numbers in."""
    return re.sub(r"[^A-Z0-9]", "", document_number.upper())


def mask_document(document_number: str) -> str:
    """"AB1234567" -> "••••••567": the last 3 characters only."""
    value = document_number.strip()
    return "•" * max(len(value) - 3, 3) + value[-3:] if len(value) > 3 else "•" * len(value)


def mask_date(iso_date: str) -> str:
    """Any date shown masked: the year only ("1990-01-31" -> "1990-••-••")."""
    return f"{iso_date[:4]}-••-••"


mask_date_of_birth = mask_date


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
