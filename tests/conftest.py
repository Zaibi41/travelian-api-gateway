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


@pytest.fixture(autouse=True)
def _no_real_notifications(request, monkeypatch):
    """Unit tests never write notifications to a real database or send email; tests marked `real_notifications` opt out
    (staff notifications only: customer emails are always recorded here instead; tests/test_customer_emails.py calls the real one)."""
    from app.services import customer_emails

    customer = []
    monkeypatch.setattr(customer_emails, "deliver", lambda reference, kind, method="card": customer.append((reference, kind, method)) or (False, "stubbed"))
    request.node.customer_emails = customer
    if request.node.get_closest_marker("real_notifications"):
        return
    from app.services import notifications

    recorded = []
    monkeypatch.setattr(notifications, "_record", lambda kind, reference, title, details: recorded.append((kind, reference, details)) or "n1")
    monkeypatch.setattr(notifications, "_send_email", lambda subject, body, html=None: False)
    request.node.notifications = recorded


def pytest_configure(config):
    config.addinivalue_line("markers", "real_notifications: write staff notifications to the test database instead of stubbing them")


@pytest.fixture(autouse=True)
def _no_existing_bookings(request, monkeypatch):
    """The double-booking check (app.services.duplicate_bookings) finds no existing booking in unit tests, which have no
    database. Tests with the disposable `database` fixture read the real bookings table."""
    from app.services import duplicate_bookings

    duplicate_bookings._in_flight.clear()
    if "database" in request.fixturenames:
        return
    from app.db import duplicate_bookings as store

    monkeypatch.setattr(store, "travelers_named", lambda names, exclude_reference=None: [])
