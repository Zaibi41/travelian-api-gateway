"""Gateway tests never depend on the real .env for Southair, and never reach the live supplier.

Every test starts with booking and ticketing off and no Southair credentials. A test that
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
         patch.object(settings, "ratehawk_endpoint", None), \
         patch.object(settings, "ratehawk_key_id", None), \
         patch.object(settings, "ratehawk_api_key", None), \
         patch.object(settings, "ratehawk_write_enabled", False), \
         patch.object(settings, "hotel_markup_percent", None):
        yield


@pytest.fixture(autouse=True)
def no_real_application_database(monkeypatch):
    """Unit tests never reach the database the developer's .env points at. Tests that need a
    database use the `database` fixture (tests/database), which replaces this with a disposable one."""
    from fastapi import HTTPException

    from app.db import agencies

    def unavailable():
        raise HTTPException(503, "Database not available in unit tests.")

    monkeypatch.setattr(agencies, "application_engine", unavailable)
