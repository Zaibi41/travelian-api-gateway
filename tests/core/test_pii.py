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
