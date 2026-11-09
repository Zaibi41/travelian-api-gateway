"""Traveler identity encryption, masking and keyed hash (app/core/pii.py)."""

import base64
import os
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from app.core import pii
from app.core.config import settings
from tests.pii_keys import new_key, pii_test_keys


def test_round_trip_and_value_is_not_plaintext():
    with pii_test_keys():
        stored = pii.encrypt("AB1234567", "booking:b1:traveler:0:document_number")
        assert "AB1234567" not in stored and pii.key_id_of(stored) == "k1"
        assert pii.decrypt(stored, "booking:b1:traveler:0:document_number") == "AB1234567"
        assert pii.encrypt("AB1234567", "ctx") != pii.encrypt("AB1234567", "ctx")  # fresh nonce


def test_value_is_bound_to_its_booking_traveler_and_field():
    with pii_test_keys():
        stored = pii.encrypt("1990-01-01", "booking:b1:traveler:0:date_of_birth")
        for other in ("booking:b2:traveler:0:date_of_birth", "booking:b1:traveler:1:date_of_birth", "booking:b1:traveler:0:document_number"):
            with pytest.raises(pii.PiiDecryptError):
                pii.decrypt(stored, other)


def test_rotation_old_values_still_decrypt_new_values_use_the_active_key():
    old, new = new_key(), new_key()
    with pii_test_keys({"k1": old}, active="k1"):
        stored_old = pii.encrypt("AB1234567", "ctx")
    with pii_test_keys({"k1": old, "k2": new}, active="k2"):
        assert pii.decrypt(stored_old, "ctx") == "AB1234567"
        assert pii.key_id_of(pii.encrypt("AB1234567", "ctx")) == "k2"
    with pii_test_keys({"k2": new}, active="k2"), pytest.raises(pii.PiiDecryptError):
        pii.decrypt(stored_old, "ctx")  # an old key removed too early: unreadable, never wrong data


def test_tampered_value_is_refused():
    with pii_test_keys():
        stored = pii.encrypt("AB1234567", "ctx")
        key_id, _, blob = stored.partition(":")
        raw = bytearray(base64.b64decode(blob))
        raw[-1] ^= 1
        with pytest.raises(pii.PiiDecryptError):
            pii.decrypt(f"{key_id}:{base64.b64encode(bytes(raw)).decode()}", "ctx")


@pytest.mark.parametrize("keys,active,hash_key", [
    (None, "k1", "ok"),                                   # no keys
    ("k1:" + base64.b64encode(os.urandom(16)).decode(), "k1", "ok"),  # 128-bit key
    ("k1:not-base64!", "k1", "ok"),
    ("k1:KEY", "k2", "ok"),                               # active id not in the list
    ("bad id:KEY", "bad id", "ok"),
    ("k1:KEY", "k1", None),                               # no hash key
    ("k1:KEY", "k1", "short"),
])
def test_missing_or_invalid_keys_are_not_configured(keys, active, hash_key):
    key = new_key()
    keys = keys.replace("KEY", key) if keys else None
    hash_value = {"ok": new_key(), "short": base64.b64encode(os.urandom(16)).decode(), None: None}[hash_key]
    with patch.object(settings, "pii_keys", SecretStr(keys) if keys else None), patch.object(settings, "pii_active_key", active), \
         patch.object(settings, "pii_hash_key", SecretStr(hash_value) if hash_value else None):
        assert pii.configured() is False
        with pytest.raises(pii.PiiNotConfigured):
            pii.encrypt("x", "ctx")


def test_masks_and_keyed_hash():
    assert pii.mask_document("AB1234567") == "••••••567"
    assert pii.mask_document("AB1") == "•••"
    assert pii.mask_date_of_birth("1990-01-31") == "1990-••-••"
    with pii_test_keys():
        assert pii.document_hash("ab 123-4567") == pii.document_hash("AB1234567")
        assert pii.document_hash("AB1234567") != pii.document_hash("AB1234568")
        first = pii.document_hash("AB1234567")
    with pii_test_keys():
        assert pii.document_hash("AB1234567") != first  # keyed: a different hash key gives a different hash
