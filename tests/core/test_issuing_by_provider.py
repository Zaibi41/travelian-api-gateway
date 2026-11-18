"""Issuing routes by provider: a SooperFare booking is never checked or ticketed through Southair, and its
checklist never shows Southair's balance. SooperFare issuing isn't built yet (its confirm call takes payment)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.field_visibility import ALL, ISSUE_TICKETS, BackOfficeUser
from app.db import issuance as store
from app.services import issuance

ADMIN = BackOfficeUser("u1", "super_admin", ALL, frozenset({ISSUE_TICKETS}))


def target(provider, status="not_issued"):
    return store.Target("b1", "TRV-1", "IKVIMS", provider, "flight", "held", status,
                        datetime.now(timezone.utc) + timedelta(hours=20), "PKR", 2207000, 2207000, "uuid-1", 2207000, [(0, "ADT")])


@pytest.mark.parametrize("call", ["issue", "verify"])
def test_a_sooperfare_booking_is_refused_before_any_supplier_call(call):
    southair = MagicMock(pnr_info=AsyncMock(), ticketing=AsyncMock(), profile=AsyncMock())
    with patch.object(settings, "southair_ticketing_enabled", True), patch.object(store, "load", return_value=target("sooperfare", "unknown" if call == "verify" else "not_issued")), \
         patch.object(store, "claim", return_value=target("sooperfare")), patch.object(store, "release") as release:
        with pytest.raises(store.IssuanceRefused) as error:
            asyncio.run(getattr(issuance, call)("TRV-1", ADMIN, client_factory=lambda: southair))
    assert error.value.status == 409 and "SooperFare's portal" in error.value.message
    if call == "issue":  # claimed, then put straight back to its previous status
        assert release.call_args.args[2] == "not_issued" and release.call_args.args[4] == {"reason_code": "provider_not_supported"}
    southair.pnr_info.assert_not_awaited()
    southair.ticketing.assert_not_awaited()


def test_ticketing_is_on_only_for_southair_bookings():
    with patch.object(settings, "southair_ticketing_enabled", True):
        assert issuance.ticketing_on("southair") and issuance.ticketing_on(None)
        assert not issuance.ticketing_on("sooperfare")
    with patch.object(settings, "southair_ticketing_enabled", False):
        assert not issuance.ticketing_on("southair")
