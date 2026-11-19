"""Cancel at SooperFare: Cancelled only when SooperFare's own record says so (live 19 Nov 2026: the cancel call answered
"Operation Successfull" while the booking stayed active)."""

import asyncio

import pytest

from app.services import sooperfare_bookings as svc
from app.vendors.flights.sooperfare.client import SooperfareError

CANCEL_OK = {"status": True, "message": "Operation Successfull", "data": {"transaction": {
    "uuid": "b6be2769-9d71-42de-be13-e5cfc83b7c08", "txn_ref_id": "txn-016", "status": "unpaid", "description": "Cancel For Airline Booking."}}}


def listing(cancelled: bool, status: str = "active"):
    return {"data": [{"uuid": "sf-1", "booking": {"reservation_status": status, "order_ref_id": "spa-0rj", "expiry_at": "2026-11-20T21:23:32+05:00"},
                      "additional": {"is_cancelled": cancelled, "is_paid": False, "is_issued": False}}]}


class FakeSooperfare:
    def __init__(self, after_cancel, cancel_error=None):
        self.after_cancel, self.cancel_error, self.calls = after_cancel, cancel_error, []

    async def cancel(self, booking_ref_id, txn_ref_id, amount, currency, remarks):
        self.calls.append(("cancel", booking_ref_id, txn_ref_id, amount, currency))
        if self.cancel_error:
            raise self.cancel_error
        return CANCEL_OK

    async def booking_list(self, filters=None):
        self.calls.append(("list",))
        return self.after_cancel


@pytest.fixture
def stored(monkeypatch):
    saved = []
    record = svc.Record("b1", "TRV-SF1", "sooperfare", "held", "OFZOJG", "sf-1", 2_407_000, "PKR", {"reservation": {"order_ref_id": "spa-0rj"}})
    monkeypatch.setattr(svc, "_load", lambda reference: record)
    monkeypatch.setattr(svc, "_save", lambda r, actor, action, details, cancel=None, status=None: saved.append((action, cancel, status)))
    return record, saved


def test_accepted_but_still_active_is_cancel_requested_not_cancelled(stored):
    _, saved = stored
    fake = FakeSooperfare(listing(False))
    state = asyncio.run(svc.cancel("TRV-SF1", "u1", lambda: fake))
    assert fake.calls[0] == ("cancel", "sf-1", "TRV-SF1", 24070.0, "PKR")
    assert state["state"] == "requested" and state["answer"]["txn_ref_id"] == "txn-016"
    action, cancel, status = saved[-1]
    assert action == "supplier_cancel_requested" and status is None      # still held


@pytest.mark.parametrize("after", [listing(True), listing(False, "cancelled")])
def test_cancelled_only_when_sooperfare_says_so(stored, after):
    _, saved = stored
    state = asyncio.run(svc.cancel("TRV-SF1", "u1", lambda: FakeSooperfare(after)))
    assert state["state"] == "confirmed" and saved[-1][0] == "supplier_cancel_confirmed" and saved[-1][2] == "cancelled"


def test_refused_cancel_shows_sooperfares_reason_and_changes_nothing(stored):
    _, saved = stored
    fake = FakeSooperfare(listing(False), SooperfareError("refused", "Booking cannot be cancelled.", status=422))
    with pytest.raises(svc.SupplierCancelError) as error:
        asyncio.run(svc.cancel("TRV-SF1", "u1", lambda: fake))
    assert "Booking cannot be cancelled." in error.value.message and "Nothing was changed" in error.value.message
    assert saved[-1][0] == "supplier_cancel_failed" and saved[-1][2] is None


def test_check_later_marks_cancelled_once_sooperfare_confirms(stored):
    record, saved = stored
    record.supplier_response["supplier_cancel"] = {"state": "requested"}
    assert asyncio.run(svc.check("TRV-SF1", "u1", lambda: FakeSooperfare(listing(False))))["state"] == "requested"
    assert saved[-1][0] == "supplier_checked" and saved[-1][2] is None
    assert asyncio.run(svc.check("TRV-SF1", "u1", lambda: FakeSooperfare(listing(True))))["state"] == "confirmed"
    assert saved[-1][2] == "cancelled"


def test_only_held_sooperfare_bookings(stored):
    record, _ = stored
    record.booking_status = "ticket_issued"
    with pytest.raises(svc.SupplierCancelError) as error:
        asyncio.run(svc.cancel("TRV-SF1", "u1", lambda: FakeSooperfare(listing(False))))
    assert error.value.status == 409


def test_confirm_shows_the_exact_request_and_never_sends_while_off(stored, monkeypatch):
    from app.core.config import settings

    record, saved = stored
    assert svc.confirm_request("TRV-SF1") == {"method": "POST", "path": "/v1/booking/confirm", "body": {
        "booking_ref_id": "sf-1", "txn_ref_id": "TRV-SF1", "amount": 24070.0, "currency": "PKR", "remarks": "Travelian booking TRV-SF1, PNR OFZOJG"}}

    class NeverCalled:
        async def confirm(self, *args):
            raise AssertionError("confirm must not be sent")

    monkeypatch.setattr(settings, "sooperfare_confirm_enabled", False)
    with pytest.raises(svc.SupplierCancelError) as error:
        asyncio.run(svc.confirm("TRV-SF1", "u1", lambda: NeverCalled()))
    assert error.value.code == "confirm_off" and saved == []
