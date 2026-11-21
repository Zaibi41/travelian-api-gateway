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


@pytest.fixture(autouse=True)
def emails(monkeypatch):
    """The customer's "Booking cancelled" email (gateway#52): recorded here, never sent."""
    from app.services import customer_emails

    sent = []

    async def send(reference, kind, *, method="card"):
        sent.append((reference, kind))

    monkeypatch.setattr(customer_emails, "send", send)
    return sent


def test_accepted_but_still_active_is_cancel_requested_not_cancelled(stored, emails):
    _, saved = stored
    fake = FakeSooperfare(listing(False))
    state = asyncio.run(svc.cancel("TRV-SF1", "u1", lambda: fake))
    assert fake.calls[0] == ("cancel", "sf-1", "TRV-SF1", 24070.0, "PKR")
    assert state["state"] == "requested" and state["answer"]["txn_ref_id"] == "txn-016"
    action, cancel, status = saved[-1]
    assert action == "supplier_cancel_requested" and status is None      # still held
    assert emails == []  # not cancelled yet, so the customer isn't told


@pytest.mark.parametrize("after", [listing(True), listing(False, "cancelled")])
def test_cancelled_only_when_sooperfare_says_so(stored, after, emails):
    _, saved = stored
    state = asyncio.run(svc.cancel("TRV-SF1", "u1", lambda: FakeSooperfare(after)))
    assert state["state"] == "confirmed" and saved[-1][0] == "supplier_cancel_confirmed" and saved[-1][2] == "cancelled"
    assert emails == [("TRV-SF1", "booking_cancelled")]


def test_refused_cancel_shows_sooperfares_reason_and_changes_nothing(stored, emails):
    _, saved = stored
    fake = FakeSooperfare(listing(False), SooperfareError("refused", "Booking cannot be cancelled.", status=422))
    with pytest.raises(svc.SupplierCancelError) as error:
        asyncio.run(svc.cancel("TRV-SF1", "u1", lambda: fake))
    assert "Booking cannot be cancelled." in error.value.message and "Nothing was changed" in error.value.message
    assert saved[-1][0] == "supplier_cancel_failed" and saved[-1][2] is None and emails == []


@pytest.mark.parametrize("failure, words", [(SooperfareError("timeout", "The flight supplier took too long to answer."), "took too long to answer"),
                                            (SooperfareError("unreachable"), "couldn't be reached"),
                                            (SooperfareError("invalid_response", status=200), "couldn't read")])
def test_no_clear_answer_is_said_in_plain_words_and_keeps_the_booking_held(stored, failure, words):
    _, saved = stored
    with pytest.raises(svc.SupplierCancelError) as error:
        asyncio.run(svc.cancel("TRV-SF1", "u1", lambda: FakeSooperfare(listing(False), failure)))
    message = error.value.message
    assert words in message and "may not have gone through" in message and "still held" in message and "Check with SooperFare" in message
    assert "(" not in message  # no error codes
    action, cancel, status = saved[-1]
    assert action == "supplier_cancel_failed" and status is None and cancel["state"] == "failed" and words in cancel["message"]


def test_check_later_marks_cancelled_once_sooperfare_confirms(stored, emails):
    record, saved = stored
    record.supplier_response["supplier_cancel"] = {"state": "requested"}
    assert asyncio.run(svc.check("TRV-SF1", "u1", lambda: FakeSooperfare(listing(False))))["state"] == "requested"
    assert saved[-1][0] == "supplier_checked" and saved[-1][2] is None and emails == []
    assert asyncio.run(svc.check("TRV-SF1", "u1", lambda: FakeSooperfare(listing(True))))["state"] == "confirmed"
    assert saved[-1][2] == "cancelled" and emails == [("TRV-SF1", "booking_cancelled")]


def test_an_email_failure_never_changes_a_confirmed_cancel(stored, monkeypatch):
    from app.services import customer_emails

    async def broken(reference, kind, *, method="card"):
        raise RuntimeError("SES down")

    monkeypatch.setattr(customer_emails, "send", broken)
    _, saved = stored
    assert asyncio.run(svc.cancel("TRV-SF1", "u1", lambda: FakeSooperfare(listing(True))))["state"] == "confirmed"
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


def test_cancel_route_checks_the_role_and_returns_sooperfares_refusal(monkeypatch):
    """The gateway decides who may cancel (issue tickets + supplier names), never the page."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import admin_bookings as routes
    from app.core.field_visibility import ALL, NONE, FieldVisibility, BackOfficeUser, require_back_office, resolve_actions

    called = []

    async def fake_cancel(reference, actor, factory=None):
        called.append((reference, actor))
        raise svc.SupplierCancelError(502, "SooperFare refused the cancel: Booking cannot be cancelled. Nothing was changed.", "cancel_failed")

    monkeypatch.setattr(svc, "cancel", fake_cancel)

    def post(viewer):
        app = FastAPI()
        app.include_router(routes.router, prefix="/v1")
        app.dependency_overrides[require_back_office] = lambda: viewer
        return TestClient(app).post("/v1/admin/bookings/TRV-SF1/supplier-cancel")

    for viewer in (BackOfficeUser("u2", "staff", NONE), BackOfficeUser("u3", "staff", FieldVisibility(supplier_names=True, cost_and_margin=False))):
        assert post(viewer).status_code == 403
    assert called == []
    answer = post(BackOfficeUser("u1", "super_admin", ALL, resolve_actions("super_admin", set())))
    assert answer.status_code == 502 and answer.json() == {
        "detail": "SooperFare refused the cancel: Booking cannot be cancelled. Nothing was changed.", "code": "cancel_failed"}
    assert called == [("TRV-SF1", "u1")]
