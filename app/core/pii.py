"""Helpers for traveler identity data (passport / ID number, date of birth, phone, email, document expiry).

Passport / ID numbers and dates of birth are encrypted at rest once the identity keys are configured (gateway#84,
docs/pii-storage.md); without keys they stay plain (sandbox). The back office gets them in full; values are never logged.
"""

from __future__ import annotations

import re
from datetime import date

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


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


# ---------- encryption at rest (gateway#84) ----------
#
# AES-256-GCM with a fresh 96-bit nonce per value; the column it belongs to ("table.column") is the associated data,
# so a ciphertext copied into another column doesn't decrypt. Stored as "<key id>:<base64 nonce + ciphertext>".
# Matching uses a keyed hash (HMAC-SHA256 with a separate key) of the normalised value: exact document numbers and
# dates of birth can still be compared in SQL without the plain value. Keys come from settings only (environment or
# Parameter Store), never from the repo; nothing here logs a key, a plain value or a ciphertext.
#
# No keys configured (all three settings empty): encryption is off and the gateway keeps the plain columns, as in the
# sandbox. Keys set but incomplete or invalid: PiiNotConfigured, so nothing is written in plain text by mistake.

import base64
import binascii
import hashlib
import hmac
import os
from dataclasses import dataclass

_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class PiiNotConfigured(Exception):
    """The identity keys are set but incomplete or invalid (the message never contains a key)."""


class PiiDecryptError(Exception):
    """A stored value can't be decrypted: unknown key id (not configured), wrong column, or tampered."""


@dataclass(frozen=True)
class Keys:
    active_id: str
    keys: dict[str, bytes]
    hash_key: bytes


def _secret(value) -> str:
    return (value.get_secret_value() if value is not None else "").strip()


def _b64(value: str) -> bytes:
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise PiiNotConfigured("An identity key is not valid base64.") from None


_cache: dict[tuple[str, str, str], Keys | None] = {}


def load_keys() -> Keys | None:
    """The configured keys, or None when encryption is off (nothing set). Cached per setting values."""
    from app.core.config import settings

    raw, active, hash_raw = _secret(settings.pii_keys), (settings.pii_active_key or "").strip(), _secret(settings.pii_hash_key)
    cache_key = (hashlib.sha256(raw.encode()).hexdigest(), active, hashlib.sha256(hash_raw.encode()).hexdigest())
    if cache_key in _cache:
        return _cache[cache_key]
    if not raw and not active and not hash_raw:
        _cache[cache_key] = None
        return None
    keys: dict[str, bytes] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        key_id, _, encoded = entry.partition(":")
        key = _b64(encoded)
        if not _KEY_ID.match(key_id) or len(key) != 32:
            raise PiiNotConfigured("PII_KEYS must list '<key id>:<base64 32-byte key>' entries.")
        keys[key_id] = key
    hash_key = _b64(hash_raw) if hash_raw else b""
    if not keys or active not in keys:
        raise PiiNotConfigured("PII_ACTIVE_KEY must name one of the PII_KEYS ids.")
    if len(hash_key) < 32:
        raise PiiNotConfigured("PII_HASH_KEY must be a base64 key of at least 32 bytes.")
    found = Keys(active, keys, hash_key)
    _cache[cache_key] = found
    return found


def encryption_on() -> bool:
    return load_keys() is not None


def seal(value: str | None, field: str, keys: Keys | None = None) -> str | None:
    """The stored form of a value for `field` ("table.column"), or None for no value. Needs keys."""
    if value is None or value == "":
        return None
    keys = keys or load_keys()
    if keys is None:
        raise PiiNotConfigured("Identity encryption is off.")
    nonce = os.urandom(12)
    sealed = AESGCM(keys.keys[keys.active_id]).encrypt(nonce, value.encode(), field.encode())
    return f"{keys.active_id}:{base64.b64encode(nonce + sealed).decode()}"


def unseal(stored: str | None, field: str, keys: Keys | None = None) -> str | None:
    if stored is None:
        return None
    keys = keys or load_keys()
    key_id, _, encoded = stored.partition(":")
    key = keys.keys.get(key_id) if keys else None
    if key is None:
        raise PiiDecryptError("The key for a stored identity value is not configured.")
    try:
        blob = base64.b64decode(encoded, validate=True)
        return AESGCM(key).decrypt(blob[:12], blob[12:], field.encode()).decode()
    except (InvalidTag, binascii.Error, ValueError):
        raise PiiDecryptError("A stored identity value could not be decrypted.") from None


def key_id_of(stored: str) -> str:
    return stored.partition(":")[0]


def blind_index(value: str | None, field: str, keys: Keys | None = None) -> str | None:
    """Keyed hash of an already normalised value for `field`, for exact matching in SQL. None when off or no value."""
    if not value:
        return None
    keys = keys or load_keys()
    if keys is None:
        return None
    return hmac.new(keys.hash_key, f"{field}\x00{value}".encode(), hashlib.sha256).hexdigest()
