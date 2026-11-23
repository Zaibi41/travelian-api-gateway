"""Traveler identity helpers (app/core/pii.py). Stored as plain text in the sandbox phase."""

from datetime import date

import pytest

from app.core import pii


def test_normalized_document():
    assert pii.normalize_document("ab 123-4567") == "AB1234567"


@pytest.mark.parametrize("expiry,status", [
    (None, None), ("2026-11-08", "expired"), ("2026-11-09", "expiring"), ("2027-05-08", "expiring"),
    ("2027-05-09", "valid"), ("2031-01-01", "valid"),
])
def test_document_validity_from_today(expiry, status):
    today = date(2026, 11, 9)
    assert pii.document_validity(date.fromisoformat(expiry) if expiry else None, today) == status


@pytest.mark.parametrize("number,id_type,country,normalized", [
    ("35202-1234567-1", "NATI", "PAK", "3520212345671"),   # CNIC: digits only, dashes and spaces dropped
    ("35202 1234567 1", "NATI", "PAK", "3520212345671"),
    ("ab 123-4567", "PSPT", "PAK", "AB1234567"),           # passport: upper-case letters and digits
    ("ab1234567", "PSPT", "GBR", "AB1234567"),
    ("x-12 34", "NATI", "GBR", "X1234"),                   # other national IDs: like a passport
])
def test_document_normalisation_for_matching(number, id_type, country, normalized):
    assert pii.normalize_document(number, id_type, country) == normalized


def test_contact_normalisation():
    assert pii.normalize_phone("92", "03001234567") == pii.normalize_phone("92", "3001234567") == "923001234567"
    assert pii.normalize_email("  Ops@Example.Invalid ") == "ops@example.invalid"


# ---------- gateway#84: encryption at rest ----------

import base64 as _b64

import pytest as _pytest
from pydantic import SecretStr as _SecretStr

from app.core import pii as _pii
from app.core.config import settings as _settings

_K1, _K2, _H = (_b64.b64encode(bytes([n]) * 32).decode() for n in (7, 8, 9))


def _keys(monkeypatch, keys=f"a:{_K1}", active="a", hash_key=_H):
    monkeypatch.setattr(_settings, "pii_keys", _SecretStr(keys) if keys is not None else None)
    monkeypatch.setattr(_settings, "pii_active_key", active)
    monkeypatch.setattr(_settings, "pii_hash_key", _SecretStr(hash_key) if hash_key is not None else None)


def test_encryption_is_off_without_any_key(monkeypatch):
    _keys(monkeypatch, keys=None, active=None, hash_key=None)
    assert _pii.load_keys() is None and not _pii.encryption_on()
    assert _pii.blind_index("AB1", "f") is None
    with _pytest.raises(_pii.PiiNotConfigured):
        _pii.seal("AB1", "f")


def test_seal_and_unseal_round_trip_with_a_fresh_nonce_and_bound_to_the_column(monkeypatch):
    _keys(monkeypatch)
    one, two = _pii.seal("AB1234567", "t.c"), _pii.seal("AB1234567", "t.c")
    assert one != two and one.startswith("a:") and "AB1234567" not in one
    assert _pii.unseal(one, "t.c") == "AB1234567"
    with _pytest.raises(_pii.PiiDecryptError):
        _pii.unseal(one, "t.other")
    tampered = one[:-4] + ("AAAA" if not one.endswith("AAAA") else "BBBB")
    with _pytest.raises(_pii.PiiDecryptError):
        _pii.unseal(tampered, "t.c")
    assert _pii.seal(None, "t.c") is None and _pii.seal("", "t.c") is None and _pii.unseal(None, "t.c") is None


def test_old_keys_still_open_after_rotation(monkeypatch):
    _keys(monkeypatch)
    old = _pii.seal("1990-01-31", "t.dob")
    _keys(monkeypatch, keys=f"a:{_K1},b:{_K2}", active="b")
    assert _pii.unseal(old, "t.dob") == "1990-01-31" and _pii.seal("x", "t.dob").startswith("b:")


def test_blind_index_is_keyed_stable_and_per_column(monkeypatch):
    _keys(monkeypatch)
    first = _pii.blind_index("AB1234567", "docs")
    assert first == _pii.blind_index("AB1234567", "docs") and len(first) == 64
    assert first != _pii.blind_index("AB1234567", "other") and first != _pii.blind_index("AB1234568", "docs")
    _keys(monkeypatch, hash_key=_K2 + "")
    assert _pii.blind_index("AB1234567", "docs") != first


@_pytest.mark.parametrize("keys,active,hash_key", [
    (f"a:{_K1}", "missing", _H),                          # active id not listed
    (f"a:{_K1}", "a", None),                              # no hash key
    (f"a:{_K1}", "a", _b64.b64encode(b"short").decode()), # hash key too short
    ("a:not-base64!!", "a", _H),                          # bad key
    (f"a:{_b64.b64encode(b'x' * 16).decode()}", "a", _H), # 16-byte key
    ("bad id!:" + _K1, "bad id!", _H),
])
def test_incomplete_or_invalid_keys_are_refused_without_echoing_them(monkeypatch, keys, active, hash_key):
    _keys(monkeypatch, keys, active, hash_key)
    with _pytest.raises(_pii.PiiNotConfigured) as error:
        _pii.load_keys()
    assert _K1 not in str(error.value) and _H not in str(error.value)
