"""Gateway tests never depend on the real .env for Southair, and never reach the live supplier.

Every test starts with booking and ticketing off, no Southair credentials and no traveler
encryption keys. A test that
needs a flag on sets it explicitly (patch.object(settings, ...)) inside the test or its fixture.
"""

from unittest.mock import patch

import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def southair_settings_from_tests_only():
    with patch.object(settings, "southair_booking_enabled", False), \
         patch.object(settings, "southair_ticketing_enabled", False), \
         patch.object(settings, "southair_base_url", None), \
         patch.object(settings, "southair_username", None), \
         patch.object(settings, "southair_password", None), \
         patch.object(settings, "pii_keys", None), patch.object(settings, "pii_active_key", None), \
         patch.object(settings, "pii_hash_key", None):
        yield
