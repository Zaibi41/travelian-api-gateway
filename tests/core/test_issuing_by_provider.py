"""Issuing by provider. A SooperFare booking is never checked or ticketed through Southair, and its checklist never
shows Southair's balance. SooperFare issuing (gateway#45): one POST /v1/booking/confirm, then SooperFare's booking/list
decides; ticket numbers for every traveler -> Ticket issued; anything else -> unknown (Check with supplier). All
SooperFare answers here are mocked: no test calls SooperFare."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.field_visibility import ALL, ISSUE_TICKETS, BackOfficeUser
from app.db import issuance as store
from app.services import issuance, sooperfare_bookings
from app.vendors.flights.sooperfare.client import SooperfareError

ADMIN = BackOfficeUser("u1", "super_admin", ALL, frozenset({ISSUE_TICKETS}))
REQUEST = {"method": "POST", "path": "/v1/booking/confirm", "body": {"booking_ref_id": "uuid-1", "txn_ref_id": "TRV-1", "amount": 22070.0,
                                                                     "currency": "PKR", "remarks": "Travelian booking TRV-1, PNR IKVIMS"}}
RECORD = sooperfare_bookings.Record("b1", "TRV-1", "sooperfare", "held", "IKVIMS", "uuid-1", 2207000, "PKR", {})


def target(provider, status="not_issued", travelers=((0, "ADT"),), test=False):
    return store.Target("b1", "TRV-1", "IKVIMS", provider, "flight", "held", status, datetime.now(timezone.utc) + timedelta(hours=20),
                        "PKR", 2207000, 2207000, "uuid-1", 2207000, list(travelers), test)


def passenger(kind, ticket):
    return {"ticket_number": ticket, "status": "issued" if ticket else "not_issued", "passenger": {"traveler_type": kind}}


def listing(*, issued=True, paid=True, status="active", legs=(("adult", "2147483647001"),), billable="22070.00"):
    """SooperFare's booking/list answer for uuid-1. legs: one tuple of (type, ticket) per leg, one pair per passenger."""
    if legs and not isinstance(legs[0][0], tuple):
        legs = (legs,)
    return {"data": [{"uuid": "uuid-1", "additional": {"is_issued": issued, "is_paid": paid, "is_cancelled": False},
                      "booking": {"reservation_status": status, "billable_price": billable, "providers": [
                          {"pnr": "IKVIMS", "legs": [{"passengers": [passenger(kind, ticket) for kind, ticket in leg]} for leg in legs]}]}}]}


#: SooperFare's record before the confirm: on hold, unpaid, not issued, at the stored supplier cost (Rs 22,070).
HELD = listing(issued=False, paid=False, legs=(("adult", None),))


def sooperfare(confirm=None, lists=None, before=HELD):
    """booking/list answers: `before` (the check before the confirm; None for a Check with supplier), then `lists`."""
    lists = ([before] if before is not None else []) + list(lists or [listing()])
    return MagicMock(confirm=confirm or AsyncMock(return_value={"status": True, "message": "Operation Successfull"}),
                     booking_list=AsyncMock(side_effect=lists + [lists[-1]] * 5))


def run_issue(sf, booking=None, **switches):
    booking = booking or target("sooperfare")
    with patch.object(settings, "sooperfare_ticketing_enabled", switches.get("ticketing", True)), \
         patch.object(settings, "sooperfare_confirm_enabled", switches.get("confirm", True)), \
         patch.object(issuance, "SOOPERFARE_READS", (0, 0, 0)), \
         patch.object(store, "load", return_value=booking), patch.object(store, "claim", return_value=booking), \
         patch.object(store, "release") as release, patch.object(store, "mark_issued") as mark_issued, \
         patch.object(sooperfare_bookings, "confirm_request", return_value=REQUEST), patch.object(sooperfare_bookings, "_load", return_value=RECORD):
        outcome = asyncio.run(issuance.issue("TRV-1", ADMIN, sooperfare_factory=lambda: sf))
    return outcome, release, mark_issued


# ---------- tickets read from SooperFare's record ----------

def test_tickets_are_read_per_passenger_and_joined_across_legs():
    assert sooperfare_bookings._tickets(listing(legs=(("adult", "111"), ("child", "222")))["data"][0]["booking"]) == [("ADT", "111"), ("CHD", "222")]
    two_legs = listing(legs=((("adult", "111"),), (("adult", "333"),)))["data"][0]["booking"]
    assert sooperfare_bookings._tickets(two_legs) == [("ADT", "111/333")]
    same = listing(legs=((("adult", "111"),), (("adult", "111"),)))["data"][0]["booking"]
    assert sooperfare_bookings._tickets(same) == [("ADT", "111")]
    partial = listing(legs=((("adult", "111"),), (("adult", None),)))["data"][0]["booking"]
    assert sooperfare_bookings._tickets(partial) == [("ADT", None)]  # a leg without a number: not recorded as issued


def test_the_recorded_unpaid_reservation_has_no_tickets():
    import json
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "docs" / "SOOPERFARE_API_RESPONSES.md").read_text()
    section = text[text.index("### 5."):text.index("### 6.")]
    answer = json.loads(re.findall(r"```json\n(.*?)\n```", section, re.S)[-1])
    state = sooperfare_bookings._sooperfare_record(answer, "49c85ade-bdfe-48a9-b8f1-2424635b702c")
    assert state["pnr"] == "OFZOJG" and state["tickets"] == [("ADT", None)] and not state["is_issued"]


# ---------- issue ----------

def test_issue_sends_one_confirm_and_records_the_ticket_numbers():
    sf = sooperfare()
    outcome, release, mark_issued = run_issue(sf)
    sf.confirm.assert_awaited_once_with("uuid-1", "TRV-1", 22070.0, "PKR", "Travelian booking TRV-1, PNR IKVIMS")
    assert outcome.result == "issued" and outcome.issuance_status == "issued" and outcome.tickets == {0: "2147483647001"}
    release.assert_not_called()
    tickets, action, details, response = mark_issued.call_args.args[2:6]
    assert tickets == {0: "2147483647001"} and action == "issuance_issued" and details["pnr"] == "IKVIMS"
    assert response["confirm"]["request"] == REQUEST and response["confirm"]["answer"]["message"] == "Operation Successfull"
    assert response["booking_list"]["tickets_given"] == 1


def test_issue_rereads_the_booking_list_until_the_numbers_appear():
    sf = sooperfare(lists=[listing(issued=False, legs=(("adult", None),)), listing()])
    outcome, _, mark_issued = run_issue(sf)
    assert outcome.result == "issued" and sf.booking_list.await_count == 3 and sf.confirm.await_count == 1  # before + 2 after


@pytest.mark.parametrize("answer,code", [
    (listing(issued=True, legs=(("adult", None),)), "sooperfare_no_tickets"),   # issued, numbers not given yet
    (listing(issued=False, legs=(("adult", None),)), "sooperfare_confirm_unclear"),  # accepted, not issued yet
    ({"data": []}, "sooperfare_confirm_unclear"),                                # not listed (unverified)
])
def test_issue_without_every_ticket_number_is_unknown_and_never_confirmed_again(answer, code):
    sf = sooperfare(lists=[answer])
    outcome, release, mark_issued = run_issue(sf)
    assert outcome.result == "unknown" and "Don't issue again" in outcome.message
    assert release.call_args.args[2:4] == ("unknown", "issuance_unknown") and release.call_args.args[4]["reason_code"] == code
    mark_issued.assert_not_called()
    assert sf.confirm.await_count == 1


def test_a_missing_ticket_for_one_of_two_travelers_is_not_recorded_as_issued():
    booking = target("sooperfare", travelers=((0, "ADT"), (1, "ADT")))
    sf = sooperfare(lists=[listing(legs=(("adult", "111"), ("adult", None)))])
    outcome, _, mark_issued = run_issue(sf, booking)
    assert outcome.result == "unknown"
    mark_issued.assert_not_called()


@pytest.mark.parametrize("http,record,expected", [
    (422, {"issued": False, "paid": False, "status": "active"}, "not_issued"),   # plain refusal, record untouched: retryable
    (422, {"issued": False, "paid": True, "status": "active"}, "unknown"),       # refused, but SooperFare shows it paid
    (422, None, "unknown"),                                                      # record can't be read
    (409, {"issued": False, "paid": False, "status": "active"}, "unknown"),     # 409/401/429 never count as "nothing happened"
    (None, {"issued": False, "paid": False, "status": "active"}, "unknown"),    # timeout
])
def test_a_failed_confirm_is_retryable_only_when_sooperfare_shows_nothing_happened(http, record, expected):
    answer = {"data": []} if record is None else listing(issued=record["issued"], paid=record["paid"], status=record["status"],
                                                         legs=(("adult", None),))
    sf = sooperfare(confirm=AsyncMock(side_effect=SooperfareError("refused" if http else "timeout", "Insufficient balance.", status=http)),
                    lists=[answer])
    outcome, release, _ = run_issue(sf)
    assert outcome.result == expected and sf.confirm.await_count == 1
    assert release.call_args.args[2] == ("not_issued" if expected == "not_issued" else "unknown")
    if http:  # the real reason is shown to staff
        assert "Insufficient balance." in outcome.message
    assert release.call_args.args[5]["confirm"]["request"] == REQUEST


def test_a_test_booking_is_refused_before_anything_is_sent():
    sf = sooperfare()
    with patch.object(settings, "sooperfare_ticketing_enabled", True), patch.object(settings, "sooperfare_confirm_enabled", True), \
         patch.object(store, "load", return_value=target("sooperfare", test=True)), patch.object(store, "claim") as claim:
        with pytest.raises(store.IssuanceRefused) as error:
            asyncio.run(issuance.issue("TRV-1", ADMIN, sooperfare_factory=lambda: sf))
    assert error.value.status == 409 and error.value.code == "test_booking" and "test booking" in error.value.message
    claim.assert_not_called()
    sf.confirm.assert_not_awaited()


def test_test_names_are_recognised():
    assert all(store.is_test_name(n) for n in ("TEST", "Test Passenger", "Ali TEST", "test"))
    assert not any(store.is_test_name(n) for n in ("Testa", "Contest", "Ali", None))


@pytest.mark.parametrize("ticketing,confirm,switch", [(False, True, "SOOPERFARE_TICKETING_ENABLED"), (True, False, "SOOPERFARE_CONFIRM_ENABLED")])
def test_with_a_switch_off_nothing_is_sent_and_the_reason_names_it(ticketing, confirm, switch):
    sf = sooperfare()
    with patch.object(settings, "sooperfare_ticketing_enabled", ticketing), patch.object(settings, "sooperfare_confirm_enabled", confirm), \
         patch.object(store, "load", return_value=target("sooperfare")), patch.object(store, "claim") as claim:
        with pytest.raises(store.IssuanceRefused) as error:
            asyncio.run(issuance.issue("TRV-1", ADMIN, sooperfare_factory=lambda: sf))
    assert switch in error.value.message and error.value.code == "ticketing_off"
    claim.assert_not_called()
    sf.confirm.assert_not_awaited()


def test_sooperfare_not_set_up_never_claims_the_booking():
    def missing():
        raise SooperfareError("not_configured")

    with patch.object(settings, "sooperfare_ticketing_enabled", True), patch.object(settings, "sooperfare_confirm_enabled", True), \
         patch.object(store, "load", return_value=target("sooperfare")), patch.object(store, "claim") as claim, \
         patch.object(sooperfare_bookings, "confirm_request", return_value=REQUEST):
        with pytest.raises(store.IssuanceRefused) as error:
            asyncio.run(issuance.issue("TRV-1", ADMIN, sooperfare_factory=missing))
    assert error.value.status == 503
    claim.assert_not_called()


def test_a_sooperfare_booking_never_reaches_southair():
    southair = MagicMock(pnr_info=AsyncMock(), ticketing=AsyncMock(), profile=AsyncMock())
    sf = sooperfare()
    with patch.object(settings, "southair_ticketing_enabled", True), patch.object(settings, "sooperfare_ticketing_enabled", True), \
         patch.object(settings, "sooperfare_confirm_enabled", True), patch.object(issuance, "SOOPERFARE_READS", (0,)), \
         patch.object(store, "load", return_value=target("sooperfare")), patch.object(store, "claim", return_value=target("sooperfare")), \
         patch.object(store, "release"), patch.object(store, "mark_issued"), \
         patch.object(sooperfare_bookings, "confirm_request", return_value=REQUEST), patch.object(sooperfare_bookings, "_load", return_value=RECORD):
        asyncio.run(issuance.issue("TRV-1", ADMIN, client_factory=lambda: southair, sooperfare_factory=lambda: sf))
    southair.pnr_info.assert_not_awaited()
    southair.ticketing.assert_not_awaited()


# ---------- Check with supplier (read-only) ----------

def run_verify(sf, status="unknown"):
    booking = target("sooperfare", status)
    with patch.object(store, "load", return_value=booking), patch.object(store, "mark_issued") as mark_issued, \
         patch.object(store, "resolve_not_issued") as resolved, patch.object(store, "mark_unknown") as unknown, patch.object(store, "log_check"), \
         patch.object(store, "last_attempt", return_value=("issuance_failed", {}, datetime.now(timezone.utc))), \
         patch.object(sooperfare_bookings, "_load", return_value=RECORD):
        outcome = asyncio.run(issuance.verify("TRV-1", ADMIN, sooperfare_factory=lambda: sf))
    return outcome, mark_issued, resolved, unknown


def test_check_records_the_tickets_sooperfare_shows():
    sf = sooperfare(before=None)
    outcome, mark_issued, _, _ = run_verify(sf)
    assert outcome.result == "issued" and mark_issued.call_args.args[2] == {0: "2147483647001"}
    sf.confirm.assert_not_awaited()


def test_check_after_a_refusal_says_safe_to_issue_again_when_sooperfare_shows_it_untouched():
    sf = sooperfare(lists=[HELD], before=None)
    outcome, _, resolved, _ = run_verify(sf, status="not_issued")
    assert outcome.result == "not_issued" and "safe to issue again" in outcome.message
    resolved.assert_not_called()
    sf.confirm.assert_not_awaited()


def test_check_after_a_confirm_never_reopens_issuing_from_the_list_alone():
    """SooperFare's list can lag a confirm: 'still on hold' keeps it unknown until staff resolve it (no double charge)."""
    sf = sooperfare(lists=[HELD], before=None)
    outcome, mark_issued, resolved, unknown = run_verify(sf, status="unknown")
    assert outcome.result == "unknown" and outcome.issuance_status == "unknown" and "Mark as not issued" in outcome.message
    resolved.assert_not_called()
    mark_issued.assert_not_called()
    sf.confirm.assert_not_awaited()


def test_check_stays_unknown_when_paid_but_not_issued():
    sf = sooperfare(lists=[listing(issued=False, paid=True, legs=(("adult", None),))], before=None)
    outcome, mark_issued, resolved, _ = run_verify(sf)
    assert outcome.result == "unknown" and "paid but not issued" in outcome.message
    mark_issued.assert_not_called()
    resolved.assert_not_called()


def test_check_when_sooperfare_doesnt_list_it_changes_nothing():
    with pytest.raises(store.IssuanceRefused) as error:
        run_verify(sooperfare(lists=[{"data": []}], before=None))
    assert error.value.status == 502


# ---------- switches and reasons ----------

def test_the_reason_names_the_supplier_and_the_switch():
    with patch.object(settings, "southair_ticketing_enabled", False), patch.object(settings, "sooperfare_ticketing_enabled", True), \
         patch.object(settings, "sooperfare_confirm_enabled", False):
        assert "Southair ticketing is switched off" in issuance.ticketing_reason("southair")
        assert "SOOPERFARE_CONFIRM_ENABLED" in issuance.ticketing_reason("sooperfare")
        assert "Tickets tab" in issuance.ticketing_reason("dhillu_sahi", "group")
    with patch.object(settings, "sooperfare_ticketing_enabled", True), patch.object(settings, "sooperfare_confirm_enabled", True):
        assert issuance.ticketing_on("sooperfare") and issuance.ticketing_reason("sooperfare") is None


def test_ticketing_is_off_for_sooperfare_by_default():
    with patch.object(settings, "southair_ticketing_enabled", True), patch.object(settings, "sooperfare_ticketing_enabled", False):
        assert issuance.ticketing_on("southair") and issuance.ticketing_on(None)
        assert not issuance.ticketing_on("sooperfare")


# ---------- checks before the confirm (it charges Travelian's balance) ----------

@pytest.mark.parametrize("before,code", [
    ({"data": []}, "sooperfare_check_failed"),                                                   # not listed
    (listing(issued=False, paid=True, legs=(("adult", None),)), "sooperfare_already_paid"),
    (listing(issued=True, paid=True), "sooperfare_already_paid"),
    (listing(issued=False, paid=False, status="expired", legs=(("adult", None),)), "sooperfare_not_held"),
    (listing(issued=False, paid=False, legs=(("adult", None),), billable="23070.00"), "supplier_cost_changed_at_sooperfare"),
])
def test_nothing_is_sent_unless_sooperfare_shows_it_held_unpaid_at_the_recorded_cost(before, code):
    sf = sooperfare(before=before)
    with pytest.raises(store.IssuanceRefused) as error:
        run_issue(sf)
    assert error.value.code == code and "Nothing was sent to SooperFare" in error.value.message
    sf.confirm.assert_not_awaited()


def test_nothing_is_sent_when_the_request_doesnt_match_the_locked_booking():
    sf = sooperfare()
    changed = store.Target("b1", "TRV-1", "IKVIMS", "sooperfare", "flight", "held", "not_issued", datetime.now(timezone.utc) + timedelta(hours=20),
                           "PKR", 2207000, 2307000, "uuid-1", 2207000, [(0, "ADT")])  # supplier cost changed after the request was built
    with pytest.raises(store.IssuanceRefused) as error:
        run_issue(sf, changed)
    assert error.value.code == "booking_changed"
    sf.confirm.assert_not_awaited()


# ---------- never automatically ----------

def test_a_paid_sooperfare_booking_is_never_issued_automatically():
    from app.services import flight_fallback, flight_payments

    booking = target("sooperfare")
    with patch.object(settings, "sooperfare_ticketing_enabled", True), patch.object(settings, "sooperfare_confirm_enabled", True), \
         patch.object(settings, "southair_ticketing_enabled", True), patch.object(flight_payments.payfast, "payments_allowed", return_value=True), \
         patch.object(store, "load", return_value=booking), patch.object(flight_payments.customer_emails if hasattr(flight_payments, "customer_emails") else __import__("app.services.customer_emails", fromlist=["x"]), "send", AsyncMock()), \
         patch.object(flight_fallback, "needs_staff", AsyncMock(return_value="needs_staff")) as staff, \
         patch.object(issuance, "issue", AsyncMock()) as issue:
        outcome = asyncio.run(flight_payments.after_verified_card_payment("TRV-1"))
    assert outcome == "needs_staff" and "never automatically" in staff.call_args.args[1]
    issue.assert_not_awaited()
