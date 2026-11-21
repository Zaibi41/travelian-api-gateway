"""gateway#109: a group hold with money recorded is never plain 'expired' at its time limit.

Paid in full, no final PNR: stays held, waiting for the final PNR (ticketing status awaiting_final_pnr), and staff can
still record it. Part-paid: paid_not_ticketed (refund or rebook) and staff are alerted. Unpaid: expired as before.
No database and no supplier: the decision functions, the view and the expiry job with a mocked store."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cli.repair_paid_group_expiry import decide
from app.core.field_visibility import ALL, NONE, BackOfficeUser, resolve_actions
from app.db import group_holds as store
from app.services import group_hold_expiry, group_holds, notifications
from app.services.group_holds import GroupHoldError
from app.services.group_ticketing_status import ticketing_status

ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))
STAFF = BackOfficeUser("u-2", "staff", NONE)
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
PAST, FUTURE = NOW - timedelta(minutes=1), NOW + timedelta(minutes=10)
TOTAL = 170_000_00


def booking(paid=0, expires=PAST, status="held", issuance="not_issued", final_pnr=None, total=TOTAL, provider="dhillu_sahi"):
    return store.GroupBooking(
        id="b-1", reference="TRV-GRP-AB12CD", pnr="DSPNR1", provider=provider, booking_status=status, issuance_status=issuance,
        payment_status="paid" if paid >= total and paid else ("part_paid" if paid else "unpaid"), hold_expires_at=expires,
        currency="PKR", total_minor=total, supplier_cost_minor=160_000_00, supplier_ref="27049",
        itinerary=[{"origin": "LHE", "destination": "JED", "airline_name": "Saudia", "departure_local": "2026-12-01T03:00"}],
        adults=2, paid_minor=paid, release={}, final_pnr=final_pnr)


# ---------- the decision ----------

@pytest.mark.parametrize(("paid", "expires", "final_pnr", "expected"), [
    (0, PAST, None, "expired"),                         # unpaid, time passed: expired (unchanged)
    (TOTAL, PAST, None, None),                          # paid in full, no final PNR: left alone
    (50_000_00, PAST, None, "paid_not_ticketed"),       # part-paid: refund or rebook, never plain expired
    (TOTAL, PAST, "K9QX2L", None),                      # final PNR recorded: nothing to end
    (0, FUTURE, None, None),                            # time not passed: nothing yet
    (TOTAL, FUTURE, None, None),
    (50_000_00, FUTURE, None, None),
])
def test_hold_end(paid, expires, final_pnr, expected):
    assert store.hold_end(booking(paid, expires, final_pnr=final_pnr), NOW) == expected


def test_hold_end_at_exactly_the_time_limit_counts_as_passed():
    assert store.hold_end(booking(0, NOW), NOW) == "expired"
    assert store.hold_end(booking(TOTAL, NOW), NOW) is None


@pytest.mark.parametrize("status", ["confirmed", "ticket_issued", "expired", "cancelled", "paid_not_ticketed", "requested"])
def test_hold_end_ignores_bookings_that_are_not_held(status):
    assert store.hold_end(booking(0, PAST, status=status), NOW) is None


@pytest.mark.parametrize("issuance", ["processing", "unknown", "issued"])
def test_hold_end_leaves_issuing_or_unclear_bookings_alone(issuance):
    assert store.hold_end(booking(0, PAST, issuance=issuance), NOW) is None


def test_waits_for_final_pnr_needs_full_payment_and_no_final_pnr():
    assert store.waits_for_final_pnr(booking(TOTAL))
    assert not store.waits_for_final_pnr(booking(TOTAL - 1))
    assert not store.waits_for_final_pnr(booking(0))
    assert not store.waits_for_final_pnr(booking(0, total=0))  # nothing received is never "paid"
    assert not store.waits_for_final_pnr(booking(TOTAL, final_pnr="K9QX2L"))
    assert not store.waits_for_final_pnr(booking(TOTAL, status="confirmed"))


# ---------- what staff see ----------

@pytest.mark.parametrize(("paid", "expected"), [(0, "expired"), (TOTAL, "held"), (50_000_00, "paid_not_ticketed")])
def test_status_after_the_time_limit(paid, expected):
    assert group_holds._status(booking(paid), NOW) == expected


def test_stored_paid_not_ticketed_reads_as_paid_not_ticketed():
    assert group_holds._status(booking(50_000_00, status="paid_not_ticketed"), NOW) == "paid_not_ticketed"


def test_paid_hold_past_its_time_reads_as_paid_awaiting_final_pnr_and_staff_can_record_it():
    with patch.object(group_holds, "provider_for", lambda vendor: None):
        view = group_holds.build_view(booking(TOTAL), None, ADMIN, NOW)
    assert view.status == "held" and view.ticketing_status == "awaiting_final_pnr"
    assert view.can_edit_tickets is True
    # Nothing more goes to the supplier after the time limit: no payment, issue or release from here.
    assert not view.can_record_payments and not view.can_issue_tickets and not view.can_release and not view.can_set_selling_price


def test_unpaid_hold_past_its_time_reads_as_expired_with_nothing_to_do():
    with patch.object(group_holds, "provider_for", lambda vendor: None):
        view = group_holds.build_view(booking(0), None, ADMIN, NOW)
    assert view.status == "expired" and view.ticketing_status is None and not view.can_edit_tickets


def test_part_paid_hold_past_its_time_reads_as_paid_not_ticketed_without_ticketing():
    with patch.object(group_holds, "provider_for", lambda vendor: None):
        view = group_holds.build_view(booking(50_000_00), None, ADMIN, NOW)
    assert view.status == "paid_not_ticketed" and view.ticketing_status is None and not view.can_edit_tickets
    assert not view.can_record_payments


def test_paid_hold_before_its_time_is_unchanged():
    with patch.object(group_holds, "provider_for", lambda vendor: None):
        view = group_holds.build_view(booking(TOTAL, FUTURE), None, ADMIN, NOW)
    assert view.status == "held" and view.ticketing_status == "awaiting_final_pnr" and view.can_edit_tickets


def test_staff_without_issue_permission_still_cannot_record_the_final_pnr():
    with patch.object(group_holds, "provider_for", lambda vendor: None):
        assert group_holds.build_view(booking(TOTAL), None, STAFF, NOW).can_edit_tickets is False


def test_ticketing_status_is_closed_for_paid_not_ticketed():
    assert ticketing_status("paid_not_ticketed", "not_issued", None, True) is None
    assert ticketing_status("held", "not_issued", None, True) == "awaiting_final_pnr"


# ---------- actions ----------

def run(coro):
    return asyncio.run(coro)


def test_record_final_pnr_on_a_paid_hold_after_its_time_is_accepted():
    paid = booking(TOTAL)
    saved = MagicMock()
    with patch.object(group_holds, "_now", return_value=NOW), patch.object(store, "load", lambda reference: paid), \
         patch.object(store, "record_tickets", saved), patch.object(group_holds, "read", AsyncMock(return_value="view")):
        assert run(group_holds.record_tickets(paid.reference, ADMIN, "K9QX2L", {1: None})) == "view"
    saved.assert_called_once()


def test_record_final_pnr_on_an_unpaid_hold_after_its_time_is_refused():
    unpaid = booking(0)
    saved = MagicMock()
    with patch.object(group_holds, "_now", return_value=NOW), patch.object(store, "load", lambda reference: unpaid), \
         patch.object(store, "record_tickets", saved), pytest.raises(GroupHoldError) as refused:
        run(group_holds.record_tickets(unpaid.reference, ADMIN, "K9QX2L", {1: None}))
    assert refused.value.code == "hold_expired" and not saved.called


def test_supplier_calls_are_refused_on_a_paid_hold_after_its_time():
    paid = booking(TOTAL, provider="skypass")
    with patch.object(group_holds, "_now", return_value=NOW), \
         patch.object(store, "expire_if_over", lambda reference, now, no_release=frozenset(): paid), \
         patch.object(group_holds, "refresh_supplier", AsyncMock(return_value=paid)), pytest.raises(GroupHoldError) as refused:
        run(group_holds.issue(paid.reference, ADMIN))
    assert refused.value.code == "hold_expired" and "final PNR" in refused.value.message


# ---------- the expiry job ----------

def test_expiry_job_alerts_staff_for_part_paid_holds_only(caplog):
    part_paid = booking(50_000_00, status="paid_not_ticketed")
    events = []

    async def booking_event(kind, **kw):
        events.append((kind, kw))

    sent = AsyncMock()
    with patch.object(store, "expire_due", lambda now, no_release=frozenset(): ["TRV-GRP-UNPAID"]), \
         patch.object(store, "paid_not_ticketed_due", lambda now, no_release=frozenset(): [part_paid]), \
         patch.object(store, "releases_due", lambda limit: []), patch("app.db.flight_expiry.expire_due", lambda now: []), \
         patch.object(notifications, "booking_event", booking_event), patch("app.services.customer_emails.send", sent):
        outcome = run(group_hold_expiry.run_once(NOW))
    assert outcome["expired"] == ["TRV-GRP-UNPAID"] and outcome["paid_not_ticketed"] == [part_paid.reference]
    assert [kind for kind, _ in events] == ["paid_not_ticketed"]
    details = events[0][1]
    assert details["reference"] == part_paid.reference and details["fare_minor"] == 50_000_00 and "Refund or rebook" in details["note"]
    # The hold-ended email goes to the unpaid one only; the part-paid customer is not told the seats were simply released.
    assert [call.args for call in sent.await_args_list] == [("TRV-GRP-UNPAID", "hold_expired")]
    assert "paid, not ticketed" in caplog.text


# ---------- the owner's repair command ----------

@pytest.mark.parametrize(("paid", "release", "cancelled", "expected"), [
    (TOTAL, {"status": "not_available"}, False, "held"),       # Dhillu & Sahi: nothing was given back
    (TOTAL, {"status": "pending", "attempts": 0}, False, "held"),
    (TOTAL, {"status": "failed", "attempts": 5}, False, "held"),  # the release never went through
    (TOTAL, {"status": "released"}, False, "paid_not_ticketed"),  # seats given back: refund or rebook
    (TOTAL, {"status": "not_available"}, True, "paid_not_ticketed"),  # cancelled at the supplier
    (50_000_00, {"status": "not_available"}, False, "paid_not_ticketed"),
    (TOTAL, None, False, "held"),
])
def test_repair_decision(paid, release, cancelled, expected):
    assert decide(paid, TOTAL, release, cancelled) == expected
