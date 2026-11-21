"""gateway#52: a group hold staff released, with the supplier's confirmation, is cancelled, so the customer gets the
"Booking cancelled" email (customer_emails decides if it is needed, once). A release the supplier didn't confirm sends
nothing. Store, supplier and email are mocked."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.field_visibility import ALL, BackOfficeUser, resolve_actions
from app.db import group_holds as store
from app.services import customer_emails, group_holds

ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))


def held():
    return store.GroupBooking("b-1", "TRV-GRP-AB12CD", "4FGYK2-4B4VYB", "skypass", "held", "not_issued", "unpaid",
                              datetime.now(timezone.utc) + timedelta(minutes=20), "PKR", 98_000_00, 98_000_00, "21319", [], 1, 98_000_00, {}, {})


@pytest.fixture
def wired(monkeypatch):
    sent, marks = [], []
    provider = MagicMock()
    provider.release = AsyncMock()

    async def open_hold(reference):
        return held(), provider

    async def send(reference, kind, *, method="card"):
        sent.append((reference, kind))

    monkeypatch.setattr(group_holds, "_open_hold", open_hold)
    monkeypatch.setattr(group_holds, "read", AsyncMock(return_value="view"))
    monkeypatch.setattr(store, "mark_released", lambda reference, actor, now: marks.append("released"))
    monkeypatch.setattr(store, "mark_release_failed_on_request", lambda reference, actor, error, now: marks.append("failed"))
    monkeypatch.setattr(customer_emails, "send", send)
    return provider, sent, marks


def test_confirmed_release_tells_the_customer(wired):
    _, sent, marks = wired
    assert asyncio.run(group_holds.release("TRV-GRP-AB12CD", ADMIN)) == "view"
    assert marks == ["released"] and sent == [("TRV-GRP-AB12CD", "booking_cancelled")]


def test_unconfirmed_release_sends_nothing(wired):
    provider, sent, marks = wired
    provider.release.side_effect = RuntimeError("supplier down")
    asyncio.run(group_holds.release("TRV-GRP-AB12CD", ADMIN))
    assert marks == ["failed"] and sent == []


def test_an_email_failure_never_changes_the_release(wired, monkeypatch):
    _, _, marks = wired
    monkeypatch.setattr(customer_emails, "send", AsyncMock(side_effect=RuntimeError("SES down")))
    assert asyncio.run(group_holds.release("TRV-GRP-AB12CD", ADMIN)) == "view" and marks == ["released"]
