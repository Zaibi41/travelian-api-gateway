"""Encryption, masking and matching for traveler identity data (date of birth, document number).

- AES-256-GCM, a fresh 96-bit nonce per value, bound to its row and field (associated data),
  so a ciphertext copied to another traveler or field doesn't decrypt.
- Keys come from the environment only (local .env, Lightsail env, later Parameter Store):
    TRAVELIAN_PII_KEYS        "<key id>:<base64 32-byte key>[,<key id>:<key>...]"
    TRAVELIAN_PII_ACTIVE_KEY  the key id new values are encrypted with
    TRAVELIAN_PII_HASH_KEY    base64 key (>= 32 bytes) for the keyed document hash
  Old key ids stay in TRAVELIAN_PII_KEYS until every row is re-encrypted (docs/pii-encryption.md).
- Stored value: "<key id>:<base64 nonce + ciphertext>". Keys, plaintext and ciphertext are never
  logged or returned except through the logged reveal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class PiiNotConfigured(Exception):
    """Encryption keys are missing or invalid; nothing with identity data may be saved."""


class PiiDecryptError(Exception):
    """A stored value can't be decrypted (unknown key id, wrong row, or tampered)."""


@dataclass(frozen=True)
class Keys:
    active_id: str
    keys: dict[str, bytes]
    hash_key: bytes


def _b64(value: str) -> bytes:
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise PiiNotConfigured() from None


def load_keys() -> Keys:
    from app.core.config import settings

    raw = settings.pii_keys.get_secret_value() if settings.pii_keys else ""
    hash_raw = settings.pii_hash_key.get_secret_value() if settings.pii_hash_key else ""
    active = (settings.pii_active_key or "").strip()
    keys: dict[str, bytes] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        key_id, _, encoded = entry.partition(":")
        key = _b64(encoded)
        if not _KEY_ID.match(key_id) or len(key) != 32:
            raise PiiNotConfigured()
        keys[key_id] = key
    hash_key = _b64(hash_raw) if hash_raw else b""
    if not keys or active not in keys or len(hash_key) < 32:
        raise PiiNotConfigured()
    return Keys(active, keys, hash_key)


def configured() -> bool:
    try:
        load_keys()
    except PiiNotConfigured:
        return False
    return True


def encrypt(value: str, context: str, keys: Keys | None = None) -> str:
    keys = keys or load_keys()
    nonce = os.urandom(12)
    sealed = AESGCM(keys.keys[keys.active_id]).encrypt(nonce, value.encode(), context.encode())
    return f"{keys.active_id}:{base64.b64encode(nonce + sealed).decode()}"


def decrypt(stored: str, context: str, keys: Keys | None = None) -> str:
    keys = keys or load_keys()
    key_id, _, encoded = stored.partition(":")
    key = keys.keys.get(key_id)
    if key is None:
        raise PiiDecryptError()
    try:
        blob = base64.b64decode(encoded, validate=True)
        return AESGCM(key).decrypt(blob[:12], blob[12:], context.encode()).decode()
    except (InvalidTag, binascii.Error, ValueError):
        raise PiiDecryptError() from None


def key_id_of(stored: str) -> str:
    return stored.partition(":")[0]


def document_hash(document_number: str, keys: Keys | None = None) -> str:
    """Keyed hash of a normalized document number, for duplicate matching without decrypting."""
    keys = keys or load_keys()
    normalized = re.sub(r"[^A-Z0-9]", "", document_number.upper())
    return hmac.new(keys.hash_key, normalized.encode(), hashlib.sha256).hexdigest()


def mask_document(document_number: str) -> str:
    """"AB1234567" -> "••••••567": the last 3 characters only."""
    value = document_number.strip()
    return "•" * max(len(value) - 3, 3) + value[-3:] if len(value) > 3 else "•" * len(value)


def mask_date_of_birth(iso_date: str) -> str:
    """"1990-01-01" -> "1990-••-••": the year only."""
    return f"{iso_date[:4]}-••-••"


def mask_date(iso_date: str) -> str:
    """Any date shown masked: the year only."""
    return f"{iso_date[:4]}-••-••"


def document_validity(expiry, today) -> str | None:
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
