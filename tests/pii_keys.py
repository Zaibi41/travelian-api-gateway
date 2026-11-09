"""Throwaway encryption keys for tests, generated per run (never real keys)."""

import base64
import os
from contextlib import contextmanager
from unittest.mock import patch

from pydantic import SecretStr

from app.core.config import settings


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@contextmanager
def pii_test_keys(keys: dict[str, str] | None = None, active: str = "k1"):
    keys = keys or {"k1": new_key()}
    with patch.object(settings, "pii_keys", SecretStr(",".join(f"{k}:{v}" for k, v in keys.items()))), \
         patch.object(settings, "pii_active_key", active), \
         patch.object(settings, "pii_hash_key", SecretStr(new_key())):
        yield
