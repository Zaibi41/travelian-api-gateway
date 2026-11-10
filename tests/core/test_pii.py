"""Masking of traveler identity data (app/core/pii.py). Stored as plain text in the sandbox phase."""

from datetime import date

import pytest

from app.core import pii


def test_masks_and_normalized_document():
    assert pii.mask_document("AB1234567") == "••••••567"
    assert pii.mask_document("AB1") == "•••"
    assert pii.mask_date_of_birth("1990-01-31") == "1990-••-••"
    assert pii.normalize_document("ab 123-4567") == "AB1234567"


@pytest.mark.parametrize("expiry,status", [
    (None, None), ("2026-11-08", "expired"), ("2026-11-09", "expiring"), ("2027-05-08", "expiring"),
    ("2027-05-09", "valid"), ("2031-01-01", "valid"),
])
def test_document_validity_from_today(expiry, status):
    today = date(2026, 11, 9)
    assert pii.document_validity(date.fromisoformat(expiry) if expiry else None, today) == status
    assert pii.mask_date("2031-01-01") == "2031-••-••"


@pytest.mark.parametrize("number,id_type,country,normalized", [
    ("35202-1234567-1", "NATI", "PAK", "3520212345671"),   # CNIC: digits only, dashes and spaces dropped
    ("35202 1234567 1", "NATI", "PAK", "3520212345671"),
    ("ab 123-4567", "PSPT", "PAK", "AB1234567"),           # passport: upper-case letters and digits
    ("ab1234567", "PSPT", "GBR", "AB1234567"),
    ("x-12 34", "NATI", "GBR", "X1234"),                   # other national IDs: like a passport
])
def test_document_normalisation_for_matching(number, id_type, country, normalized):
    assert pii.normalize_document(number, id_type, country) == normalized


def test_contact_normalisation_and_masks():
    assert pii.normalize_phone("92", "03001234567") == pii.normalize_phone("92", "3001234567") == "923001234567"
    assert pii.normalize_email("  Ops@Example.Invalid ") == "ops@example.invalid"
    assert pii.mask_phone("92", "3001234567") == "+92 •••••••567"
    assert pii.mask_email("ops@example.invalid") == "o•••@example.invalid"
    assert pii.mask_phone(None, None) is None and pii.mask_email(None) is None
