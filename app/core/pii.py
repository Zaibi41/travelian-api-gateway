"""Masking for traveler identity data (date of birth, passport / ID number, document expiry).

Sandbox phase: these values are stored as plain text (docs/pii-storage.md). Every read the
gateway returns is masked here; the full values come only from the logged reveal endpoint.
Values are never logged. Protecting them in the database is a go-live blocker (docs/roadmap.md
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


def mask_phone(phone_code: str | None, phone: str | None) -> str | None:
    """"+92 •••••••567": the country code and last 3 digits only."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    return f"+{phone_code} " * bool(phone_code) + "•" * max(len(digits) - 3, 3) + digits[-3:]


def mask_email(email: str | None) -> str | None:
    """"a•••@example.com": the first character and the domain only."""
    if not email or "@" not in email:
        return None
    local, domain = email.strip().split("@", 1)
    return f"{local[:1]}•••@{domain}"


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
