"""Travelian's own group hold time (GROUP_HOLD_MINUTES) and the expiry job: expiry 30 minutes after
the hold, auto-release at expiry, a failed release retried and flagged for Admin / Super Admin only,
payments and issuing refused after our expiry, Release hold with a supplier failure. Fake provider:
nothing reaches a real supplier."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db import booking_payments
from app.services import group_hold_expiry, group_holds
from app.services.group_holds import GroupHoldError
from tests.database.test_booking_people import ACTOR
from tests.database.test_group_holds import ADMIN, STAFF, SUPPLIER_REF, VENDOR, FakeProvider, inventory, pay, request

T0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def fake(database):
    connection, metadata, _ = database
    if not connection.execute(text("SELECT 1 FROM users WHERE id=:id"), {"id": ACTOR}).first():
        connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    provider = FakeProvider(supports_issue=False)
    # The fake provider releases; the real Dhillu & Sahi has no release call (NO_RELEASE_SOURCES), tested in
    # tests/group_ticketing/test_dhillu_sahi_hold.py.
    with patch.object(group_holds, "NO_RELEASE_SOURCES", set()), patch.object(group_hold_expiry, "NO_RELEASE_SOURCES", set()), patch.dict(group_holds.PROVIDERS, {VENDOR: lambda: provider}), \
         patch.object(settings, "group_ticketing_markup_percent", Decimal("5")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0), \
         patch.object(settings, "group_hold_minutes", 30), patch.object(settings, "group_hold_release_attempts", 3):
        yield provider


def run(coro):
    return asyncio.run(coro)


def place_at(moment=T0):
    with patch.object(group_holds, "_now", return_value=moment):
        return run(group_holds.hold(request(), ADMIN, fetch=AsyncMock(return_value=inventory())))


def releases(provider):
    return [call for call in provider.calls if isinstance(call, tuple) and call[0] == "release"]


def test_expiry_is_hold_minutes_after_the_hold_and_ignores_any_supplier_value(fake, database):
    view = place_at()
    assert view.hold_expires_at == T0 + timedelta(minutes=30)
    connection, _, _ = database
    stored = connection.scalar(text("SELECT hold_expires_at FROM bookings WHERE reference = :r"), {"r": view.booking_id})
    assert stored == T0 + timedelta(minutes=30)
    with patch.object(settings, "group_hold_minutes", 45):
        later = place_at(T0)
    assert later.hold_expires_at == T0 + timedelta(minutes=45)


def test_expiry_job_expires_and_releases_at_the_supplier_once(fake, database):
    view = place_at()
    nothing = run(group_hold_expiry.run_once(T0 + timedelta(minutes=29)))
    group = lambda outcome: {k: outcome[k] for k in ("expired", "released", "failed")}  # noqa: E731  (the job also reports flights)
    assert group(nothing) == {"expired": [], "released": [], "failed": []} and releases(fake) == []
    done = run(group_hold_expiry.run_once(T0 + timedelta(minutes=30)))
    assert group(done) == {"expired": [view.booking_id], "released": [view.booking_id], "failed": []}
    assert releases(fake) == [("release", SUPPLIER_REF)]
    expired = run(group_holds.read(view.booking_id, ADMIN))
    assert expired.status == "expired" and not expired.supplier_release_failed
    assert not expired.can_record_payments and not expired.can_issue_tickets
    # Nothing left to do: no second release.
    assert group(run(group_hold_expiry.run_once(T0 + timedelta(minutes=31)))) == {"expired": [], "released": [], "failed": []}
    assert len(releases(fake)) == 1
    connection, _, _ = database
    actions = connection.execute(text("SELECT action FROM booking_events e JOIN bookings b ON b.id = e.booking_id WHERE b.reference = :r"
                                      " ORDER BY e.created_at"), {"r": view.booking_id}).scalars().all()
    assert [a for a in actions if a not in ("hold_requested", "fare_terms_acknowledged")] == ["hold_created", "hold_expired", "supplier_released"]


def test_failed_release_is_retried_logged_and_flagged_for_admin_only(fake, caplog):
    view = place_at()
    fake.release_errors = 5  # the supplier keeps failing
    after = T0 + timedelta(minutes=30)
    first = run(group_hold_expiry.run_once(after))
    assert first["failed"] == [view.booking_id]
    assert "Group hold release failed" in caplog.text
    flagged = run(group_holds.read(view.booking_id, ADMIN))
    assert flagged.status == "expired" and flagged.supplier_release_failed is True
    staff = run(group_holds.read(view.booking_id, STAFF))
    assert staff.supplier_release_failed is None and "release_failed" not in staff.model_dump_json(exclude_none=True)
    # Retried on the next runs, up to GROUP_HOLD_RELEASE_ATTEMPTS (3), then left for a person.
    run(group_hold_expiry.run_once(after + timedelta(seconds=30)))
    run(group_hold_expiry.run_once(after + timedelta(seconds=60)))
    run(group_hold_expiry.run_once(after + timedelta(seconds=90)))
    assert len(releases(fake)) == 3
    assert run(group_holds.read(view.booking_id, ADMIN)).supplier_release_failed is True


def test_a_retry_that_succeeds_clears_the_flag(fake):
    view = place_at()
    fake.release_errors = 1
    run(group_hold_expiry.run_once(T0 + timedelta(minutes=30)))
    assert run(group_holds.read(view.booking_id, ADMIN)).supplier_release_failed is True
    second = run(group_hold_expiry.run_once(T0 + timedelta(minutes=31)))
    assert second["released"] == [view.booking_id]
    assert run(group_holds.read(view.booking_id, ADMIN)).supplier_release_failed is False


def test_payment_and_issue_are_refused_after_our_expiry_even_if_the_supplier_still_holds(fake, database):
    view = place_at(datetime.now(timezone.utc) - timedelta(minutes=31))  # our 30 minutes are over; nothing released yet
    assert releases(fake) == []
    with pytest.raises(booking_payments.PaymentRefused) as refused:
        pay(view.booking_id, 1_000_00)
    assert refused.value.status == 409 and "expired" in refused.value.message
    fake.supports_issue = True
    with pytest.raises(GroupHoldError) as error:
        run(group_holds.issue(view.booking_id, ADMIN))
    assert error.value.code == "hold_expired"


def test_release_hold_releases_at_the_supplier(fake):
    view = place_at(datetime.now(timezone.utc))
    released = run(group_holds.release(view.booking_id, ADMIN))
    assert released.status == "released" and releases(fake) == [("release", SUPPLIER_REF)]
    assert not released.supplier_release_failed


def test_release_hold_with_a_supplier_failure_shows_expired_flagged_and_the_job_retries(fake):
    view = place_at(datetime.now(timezone.utc))
    fake.release_errors = 1
    answered = run(group_holds.release(view.booking_id, ADMIN))
    assert answered.status == "expired" and answered.supplier_release_failed is True
    assert not answered.can_record_payments
    retried = run(group_hold_expiry.run_once(datetime.now(timezone.utc)))
    assert retried["released"] == [view.booking_id]
    assert run(group_holds.read(view.booking_id, ADMIN)).supplier_release_failed is False


def test_issue_is_refused_for_a_provider_without_issuing(fake):
    view = place_at(datetime.now(timezone.utc))
    pay(view.booking_id, 210_000_00)
    held = run(group_holds.read(view.booking_id, ADMIN))
    assert not held.can_issue_tickets and held.issue_unavailable_reason == "Issuing is not available for this provider yet"
    with pytest.raises(GroupHoldError) as error:
        run(group_holds.issue(view.booking_id, ADMIN))
    assert error.value.code == "issue_not_supported" and "price" not in fake.calls
