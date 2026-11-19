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
    if call == "issue":  # refused before anything is claimed (SooperFare issuing waits for the owner's approval)
        release.assert_not_called()
    southair.pnr_info.assert_not_awaited()
    southair.ticketing.assert_not_awaited()


def test_the_reason_names_the_supplier_and_the_switch():
    with patch.object(settings, "southair_ticketing_enabled", False), patch.object(settings, "sooperfare_ticketing_enabled", True), \
         patch.object(settings, "sooperfare_confirm_enabled", False):
        assert "Southair ticketing is switched off" in issuance.ticketing_reason("southair")
        assert "SOOPERFARE_CONFIRM_ENABLED" in issuance.ticketing_reason("sooperfare")
        assert "Tickets tab" in issuance.ticketing_reason("dhillu_sahi", "group")
    with patch.object(settings, "sooperfare_ticketing_enabled", True), patch.object(settings, "sooperfare_confirm_enabled", True):
        assert issuance.ticketing_on("sooperfare")


def test_sooperfare_issue_sends_one_confirm_and_waits_for_ticket_numbers():
    from app.services import sooperfare_bookings

    confirm = AsyncMock(return_value={"status": True, "message": "Operation Successfull"})
    sf = MagicMock(confirm=confirm, booking_list=AsyncMock(return_value={"data": [{"uuid": "uuid-1", "booking": {"reservation_status": "active"},
                                                                                    "additional": {"is_issued": True}}]}))
    request = {"method": "POST", "path": "/v1/booking/confirm", "body": {"booking_ref_id": "uuid-1", "txn_ref_id": "TRV-1", "amount": 22070.0,
                                                                         "currency": "PKR", "remarks": "Travelian booking TRV-1, PNR IKVIMS"}}
    record = sooperfare_bookings.Record("b1", "TRV-1", "sooperfare", "held", "IKVIMS", "uuid-1", 2207000, "PKR", {})
    with patch.object(settings, "sooperfare_ticketing_enabled", True), patch.object(settings, "sooperfare_confirm_enabled", True), \
         patch.object(store, "load", return_value=target("sooperfare")), patch.object(store, "claim", return_value=target("sooperfare")), \
         patch.object(store, "release") as release, patch.object(sooperfare_bookings, "confirm_request", return_value=request), \
         patch.object(sooperfare_bookings, "_load", return_value=record):
        outcome = asyncio.run(issuance.issue("TRV-1", ADMIN, sooperfare_factory=lambda: sf))
    confirm.assert_awaited_once_with("uuid-1", "TRV-1", 22070.0, "PKR", "Travelian booking TRV-1, PNR IKVIMS")
    assert outcome.result == "unknown" and "Record the ticket numbers" in outcome.message
    assert release.call_args.args[2:4] == ("unknown", "issuance_unknown") and release.call_args.args[4]["reason_code"] == "sooperfare_confirmed"


def test_ticketing_is_on_only_for_southair_bookings():
    with patch.object(settings, "southair_ticketing_enabled", True):
        assert issuance.ticketing_on("southair") and issuance.ticketing_on(None)
        assert not issuance.ticketing_on("sooperfare")
    with patch.object(settings, "southair_ticketing_enabled", False):
        assert not issuance.ticketing_on("southair")
