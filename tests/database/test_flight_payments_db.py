"""Website flight payments against PostgreSQL (D43, D45, D48, D49), with a fake Southair and PayFast results signed with a
test key. Mirrors test_public_booking_db.test_forged_or_unverified_payfast_never_pays_or_issues for flights, and follows
each method through the statuses the admin booking detail shows: requested -> held -> paid -> issued."""

import asyncio
import hashlib
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from app.core.config import settings
from app.core.field_visibility import ALL
from app.db import booking_payments, booking_people, bookings as dashboard, issuance as issuance_store, payment_accounts, public_bookings
from app.services import admin_bookings, flight_payments, issuance, public_booking
from app.vendors.flights.southair.client import SouthairError
from tests.database.test_booking_people import CUSTOMER, TRAVELERS, save
from tests.database.test_issuance import APPROVER, ISSUED, FakeSouthair
from tests.southair.fixtures import pnr_info

pytestmark = pytest.mark.real_notifications  # staff alerts are read back from admin_notifications

MERCHANT, KEY = "14833", "test-secured-key"
TOTAL = 46_700  # Rs 467 selling; the fake supplier's total is Rs 457.70 (tests.southair.fixtures)


def signed(basket, err_code="000", **extra):
    digest = hashlib.sha256(f"{basket}|{KEY}|{MERCHANT}|{err_code}".encode()).hexdigest()
    return {"basket_id": basket, "err_code": err_code, "validation_hash": digest, **extra}


@pytest.fixture
def website_hold(database):
    """A website Southair flight hold with three hours left, PayFast configured with the test key."""
    connection, metadata, _ = database
    reference = save(connection, metadata, hold_expires_at=datetime.now(timezone.utc) + timedelta(hours=3), actor_user_id=None)
    public_bookings.mark_website_reference(reference, public_bookings.token_hash("guest-token-123"))
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)):
        yield connection, reference


@pytest.fixture(autouse=True)
def production_payfast(monkeypatch):
    """Automatic issuing only runs on production PayFast (the UAT guard has its own tests): any host but ipguat counts."""
    monkeypatch.setattr(settings, "payfast_base_url", "https://payfast-production.example")


@pytest.fixture(autouse=True)
def no_real_rehold(monkeypatch):
    """Re-holding searches the suppliers: here it is answered by a stub (tests that need it set their own). Never a real call."""
    from app.services import flight_fallback

    calls = []

    async def rehold(reference, client_factory=None, sooperfare_factory=None):
        calls.append(reference)
        return False, "the same flights and fare aren't offered any more"

    monkeypatch.setattr(flight_fallback, "rehold", rehold)
    return calls


def tickets_sent(fake):
    return sum(1 for call in fake.calls if call[0] == "ticketing")


def admin_statuses(reference):
    """What the admin booking detail shows (booking, payment, issuance)."""
    row = admin_bookings.find_booking(dashboard.list_dashboard_bookings(), reference)
    statuses = admin_bookings.build_detail(row, ALL, booking_people.read(reference), today=date.today()).statuses
    return statuses.booking, statuses.payment, statuses.issuance


def guest(reference):
    return asyncio.run(public_booking.booking_view(reference))


def red_alerts(connection, reference):
    rows = connection.execute(sa.text("SELECT kind, title, details FROM admin_notifications WHERE reference = :r AND kind LIKE 'paid_%'"
                                      " ORDER BY created_at"), {"r": reference}).mappings().all()
    return [dict(row) for row in rows]


def notifications(connection, reference):
    return connection.scalars(sa.text("SELECT kind FROM admin_notifications WHERE reference = :r ORDER BY created_at"), {"r": reference}).all()


def pay(reference, basket, params, started):
    """Apply a PayFast result the way the notify/return routes do; `started` collects the references sent to issuance."""
    with patch.object(flight_payments, "start_after_verified_card_payment", started.append):
        return asyncio.run(public_booking.payfast_result(params, "notification"))


FORGED = [
    lambda basket: {"basket_id": basket, "err_code": "000", "transaction_amount": "467.00", "validation_hash": "0" * 64},
    lambda basket: {"basket_id": basket, "err_code": "000",
                    "validation_hash": hashlib.sha256(f"{basket}|not-the-key|{MERCHANT}|000".encode()).hexdigest()},
    lambda basket: {**signed(basket, "002"), "err_code": "000"},
    lambda basket: {"basket_id": basket, "err_code": "000", "transaction_amount": "467.00"},
]


def test_requested_flight_shows_requested_unpaid_not_issued(database):
    connection, metadata, _ = database
    from app.db import supplier_bookings

    reference = supplier_bookings.save_request(
        provider="southair", currency="PKR", selling_total_minor=TOTAL, supplier_total_minor=45_770, actor_user_id=None,
        customer=CUSTOMER, travelers=TRAVELERS, channel="website", itinerary=[{"direction": "outbound", "origin": "KHI", "destination": "LHE"}], details={})
    assert admin_statuses(reference) == ("requested", "unpaid", "not_issued")
    view = guest(reference)
    assert (view.status, view.can_pay, view.final_pnr) == ("requested", False, None)


def test_forged_or_unverified_payfast_never_pays_or_issues_a_flight(website_hold):
    """Before and after the change: no forged result records a payment, and with ticketing ON and a supplier that would
    issue, nothing is ever sent to it."""
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-FORGE", TOTAL, "PKR")
    fake = FakeSouthair([pnr_info(), ISSUED])
    started = []
    with patch.object(settings, "southair_ticketing_enabled", True):
        for forge in FORGED:
            assert pay(reference, "TRV-F-FORGE", forge("TRV-F-FORGE"), started) is None
        # Even called directly on an unpaid booking, the automatic step issues nothing.
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "pending"
    assert started == [] and fake.calls == []
    assert connection.scalar(sa.text("SELECT COUNT(*) FROM booking_payments b JOIN bookings k ON k.id = b.booking_id"
                                     " WHERE k.reference = :r"), {"r": reference}) == 0
    assert admin_statuses(reference) == ("held", "unpaid", "not_issued")
    assert "Full payment isn't recorded yet." in issuance_store.blockers(issuance_store.load(reference), datetime.now(timezone.utc), 60)
    view = guest(reference)
    assert view.status == "held" and view.can_pay and view.final_pnr is None and view.tickets == []


def test_card_ticketing_on_verified_payment_issues_with_the_real_pnr_and_tickets(website_hold):
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-CARD", TOTAL, "PKR")
    started = []
    with patch.object(settings, "southair_ticketing_enabled", True):
        outcome = pay(reference, "TRV-F-CARD", signed("TRV-F-CARD", transaction_id="T1", transaction_amount="467.00"), started)
        assert (outcome.status, outcome.payment_status, started) == ("paid", "paid", [reference])
        assert admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
        assert guest(reference).status == "processing"  # payment received, issuance in progress
        fake = FakeSouthair([pnr_info(), ISSUED])
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "issued"
        # A repeat of the same verified result changes nothing and starts nothing.
        again = pay(reference, "TRV-F-CARD", signed("TRV-F-CARD", transaction_id="T1", transaction_amount="467.00"), started)
    assert again.status == "already_paid" and started == [reference]
    assert sum(1 for call in fake.calls if call[0] == "ticketing") == 1
    assert admin_statuses(reference) == ("ticket_issued", "paid", "issued")
    view = guest(reference)
    assert (view.status, view.final_pnr, view.tickets) == ("issued", "63B55C", ["2147483647001", "2147483647002"])
    actors = connection.scalars(sa.text("SELECT e.actor_user_id FROM booking_events e JOIN bookings b ON b.id = e.booking_id"
                                        " WHERE b.reference = :r AND e.action LIKE 'issuance%'"), {"r": reference}).all()
    assert actors == [None, None]  # issued by the gateway itself, not by a staff user
    assert notifications(connection, reference)[-1] == "auto_issued"


def test_card_ticketing_off_is_paid_issuance_pending_with_a_red_alert(website_hold):
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-OFF", TOTAL, "PKR")
    fake = FakeSouthair([pnr_info()])  # the read-only hold check: still booked
    with patch.object(settings, "southair_ticketing_enabled", False):
        assert pay(reference, "TRV-F-OFF", signed("TRV-F-OFF", transaction_id="PF-778899", transaction_amount="467.00"), []).status == "paid"
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "needs_staff"
        view = guest(reference)
    assert fake.calls == ["pnr_info"] and tickets_sent(fake) == 0  # only the read-only check
    assert admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
    assert (view.status, view.final_pnr, view.tickets) == ("confirmed", None, [])  # payment received, issuance pending
    alert = red_alerts(connection, reference)[-1]
    assert alert["kind"] == "paid_not_issued"
    assert alert["title"] == f"URGENT: Payment received, ticket NOT issued — {reference}"
    details = alert["details"]
    assert details["urgent"] is True and details["amount"] == "Rs 467" and details["payfast_transaction_id"] == "PF-778899"
    assert details["supplier"] == "Southair" and details["pnr"] == "63B55C" and details["hold_status"].startswith("active")
    assert details["passenger"] == "MR Sample Passenger, MISS Baby Passenger" and "switched off" in details["reason"]
    assert "Confirm with supplier, issue the ticket, and share it with the customer." in details["note"]


def test_card_supplier_failure_is_never_shown_as_issued(website_hold):
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-FAIL", TOTAL, "PKR")
    fake = FakeSouthair([pnr_info(), pnr_info(), pnr_info()], ticketing=SouthairError("ticketing_failed", "x"))  # before, after, hold check
    with patch.object(settings, "southair_ticketing_enabled", True):
        assert pay(reference, "TRV-F-FAIL", signed("TRV-F-FAIL", transaction_amount="467.00"), []).status == "paid"
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "needs_staff"
        view = guest(reference)
    assert admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
    assert (view.status, view.final_pnr, view.tickets) == ("processing", None, [])  # issuance in progress; no made-up PNR
    assert connection.scalars(sa.text("SELECT ticket_number FROM booking_travelers t JOIN bookings b ON b.id = t.booking_id"
                                      " WHERE b.reference = :r"), {"r": reference}).all() == [None, None]
    alert = red_alerts(connection, reference)[-1]
    assert alert["kind"] == "paid_not_issued" and "didn't issue the ticket" in alert["details"]["reason"]


def test_card_insufficient_supplier_balance_refuses_before_sending(website_hold):
    """Southair UAT has a zero balance: the gateway refuses before any ticketing call, and staff are alerted."""
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-BAL", TOTAL, "PKR")
    fake = FakeSouthair([pnr_info(), pnr_info()], balance="0.00")
    with patch.object(settings, "southair_ticketing_enabled", True):
        pay(reference, "TRV-F-BAL", signed("TRV-F-BAL", transaction_amount="467.00"), [])
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "needs_staff"
    assert tickets_sent(fake) == 0
    assert admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
    assert "Not enough supplier balance" in red_alerts(connection, reference)[-1]["details"]["reason"]


@pytest.mark.parametrize("method", ["bank_transfer", "jazzcash"])
def test_bank_transfer_and_jazzcash_need_staff_to_record_and_issue(website_hold, method):
    """D49: recording the money (staff) never issues; issuing is a separate staff action."""
    connection, reference = website_hold
    account = payment_accounts.add(method, "Travelian receiving account")
    started = []
    with patch.object(settings, "southair_ticketing_enabled", True), \
         patch.object(flight_payments, "start_after_verified_card_payment", started.append):
        booking_payments.record(reference, amount_minor=TOTAL, currency="PKR", method=method, account_id=account.id, payer_reference="TXN-1",
                                received_at=datetime.now(timezone.utc), actor_user_id=None)
        assert started == []
        assert admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
        assert guest(reference).status == "processing"
        fake = FakeSouthair([pnr_info(), ISSUED])
        assert asyncio.run(issuance.issue(reference, APPROVER, client_factory=lambda: fake)).result == "issued"  # the staff click
    assert admin_statuses(reference) == ("ticket_issued", "paid", "issued")
    assert guest(reference).final_pnr == "63B55C"


# ---------- payment after the hold ended; failures for good (paid, not ticketed) ----------

def test_verified_payment_after_the_hold_expired_tries_one_rehold_then_hold_lost(database, no_real_rehold):
    """Paid after the hold expired: nothing sent for ticketing; one re-hold of the same fare; it fails -> 'paid_not_ticketed'
    ("Paid, not ticketed: refund or rebook"), the "hold lost" red alert, and the guest is told the team will contact them."""
    connection, metadata, _ = database
    reference = save(connection, metadata, hold_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5), actor_user_id=None)
    public_bookings.mark_website_reference(reference, public_bookings.token_hash("guest-token-123"))
    fake = FakeSouthair([])
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)), \
         patch.object(settings, "southair_ticketing_enabled", True):
        public_bookings.create_attempt(reference, "TRV-F-LATE", TOTAL, "PKR")
        assert pay(reference, "TRV-F-LATE", signed("TRV-F-LATE", transaction_amount="467.00"), []).status == "paid"
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "paid_not_ticketed"
        asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake))  # a repeat changes nothing
        view = guest(reference)
    assert fake.calls == [] and no_real_rehold == [reference]  # expired by time (no supplier read); re-hold tried once only
    assert admin_statuses(reference) == ("paid_not_ticketed", "paid", "not_issued")
    action = connection.scalar(sa.text("SELECT supplier_response -> 'action_needed' FROM bookings WHERE reference = :r"), {"r": reference})
    assert action["reason"] == "hold_lost"
    alerts = red_alerts(connection, reference)
    assert [a["kind"] for a in alerts] == ["paid_hold_lost"] and "refund the customer or rebook" in alerts[0]["details"]["note"].lower()
    events = connection.scalars(sa.text("SELECT e.action FROM booking_events e JOIN bookings b ON b.id = e.booking_id WHERE b.reference = :r"),
                                {"r": reference}).all()
    assert "rehold_attempt" in events and "rehold_failed" in events
    assert (view.status, view.final_pnr, view.can_pay) == ("paid_not_ticketed", None, False)


def test_payment_inside_the_ticketing_margin_keeps_the_live_hold_for_staff(database):
    connection, metadata, _ = database
    reference = save(connection, metadata, hold_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30), actor_user_id=None)
    fake = FakeSouthair([pnr_info()])
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)), \
         patch.object(settings, "southair_ticketing_enabled", True):
        public_bookings.create_attempt(reference, "TRV-F-EDGE", TOTAL, "PKR")
        pay(reference, "TRV-F-EDGE", signed("TRV-F-EDGE", transaction_amount="467.00"), [])
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "needs_staff"
    assert tickets_sent(fake) == 0 and admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
    assert red_alerts(connection, reference)[-1]["kind"] == "paid_not_issued"


def test_supplier_refusing_goes_to_staff_with_a_red_alert(website_hold):
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-GONE", TOTAL, "PKR")
    fake = FakeSouthair([pnr_info(can_ticketing=False), pnr_info(can_ticketing=False)])  # issue pre-check, then the hold check
    with patch.object(settings, "southair_ticketing_enabled", True):
        pay(reference, "TRV-F-GONE", signed("TRV-F-GONE", transaction_amount="467.00"), [])
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "needs_staff"
    assert tickets_sent(fake) == 0
    assert admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
    assert "won't ticket this PNR" in red_alerts(connection, reference)[-1]["details"]["reason"]


def test_rehold_success_keeps_the_booking_under_the_new_pnr(website_hold, monkeypatch):
    """The hold dropped at the supplier (SooperFare shows it no longer active): one re-hold succeeds; staff still get the red
    alert (ticket not issued) with the new hold."""
    from app.db import supplier_bookings
    from app.services import flight_fallback

    connection, reference = website_hold
    connection.execute(sa.text("UPDATE bookings SET provider_id = 'sooperfare', supplier_ref = 'uuid-1' WHERE reference = :r"), {"r": reference})
    later = datetime.now(timezone.utc) + timedelta(hours=5)

    async def rehold(ref, client_factory=None, sooperfare_factory=None):
        supplier_bookings.replace_hold(ref, provider="sooperfare", pnr="NEWPNR", supplier_ref="uuid-2", hold_expires_at=later,
                                       supplier_total_minor=45_000, selling_total_minor=TOTAL, supplier_response={"booking": "x"})
        return True, f"held again: new PNR NEWPNR, expires {later:%d %b %Y %H:%M} UTC"

    monkeypatch.setattr(flight_fallback, "rehold", rehold)

    class Listing:
        async def booking_list(self):
            return {"data": [{"uuid": "uuid-1", "booking": {"reservation_status": "expired"}}]}

    public_bookings.create_attempt(reference, "TRV-F-REHOLD", TOTAL, "PKR")
    pay(reference, "TRV-F-REHOLD", signed("TRV-F-REHOLD", transaction_amount="467.00"), [])
    assert asyncio.run(flight_payments.after_verified_card_payment(reference, sooperfare_factory=Listing)) == "needs_staff"
    row = connection.execute(sa.text("SELECT pnr, supplier_ref, supplier_cost_minor, booking_status, supplier_response ->> 'previous_pnr' AS previous"
                                     " FROM bookings WHERE reference = :r"), {"r": reference}).one()
    assert tuple(row) == ("NEWPNR", "uuid-2", 45_000, "held", "63B55C")
    assert admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
    alert = red_alerts(connection, reference)[-1]
    assert alert["kind"] == "paid_not_issued" and "NEWPNR" in alert["details"]["hold_status"] and alert["details"]["pnr"] == "NEWPNR"


def test_staff_issue_goes_ahead_after_a_red_alert(website_hold):
    """'Paid, issuance pending' is shown, not stored: staff issue from it as from any held booking."""
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-STAFF", TOTAL, "PKR")
    with patch.object(settings, "southair_ticketing_enabled", False):
        pay(reference, "TRV-F-STAFF", signed("TRV-F-STAFF", transaction_amount="467.00"), [])
        asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: FakeSouthair([pnr_info()])))
    with patch.object(settings, "southair_ticketing_enabled", True):
        assert asyncio.run(issuance.issue(reference, APPROVER, client_factory=lambda: FakeSouthair([pnr_info(), ISSUED]))).result == "issued"
    assert admin_statuses(reference) == ("ticket_issued", "paid", "issued")


# ---------- stuck issuance recovery ----------

LATER = timedelta(minutes=11)  # past FLIGHT_ISSUANCE_STUCK_MINUTES (10)


def _paid_by_card(reference, basket):
    public_bookings.create_attempt(reference, basket, TOTAL, "PKR")
    assert pay(reference, basket, signed(basket, transaction_amount="467.00"), []).status == "paid"


def recover(fake, at=None):
    return asyncio.run(flight_payments.recover(now=at or datetime.now(timezone.utc) + LATER, client_factory=lambda: fake))


def test_restart_mid_issue_is_checked_with_the_supplier_then_issued_once(website_hold):
    """The gateway died while 'processing'. Recovery never re-sends blind: pnr_info first (no ticket), then one issue."""
    connection, reference = website_hold
    with patch.object(settings, "southair_ticketing_enabled", True):
        _paid_by_card(reference, "TRV-F-STUCK")
        connection.execute(sa.text("UPDATE bookings SET issuance_status = 'processing' WHERE reference = :r"), {"r": reference})
        assert recover(FakeSouthair([]), at=datetime.now(timezone.utc) + timedelta(minutes=5)) == {}  # not stuck long enough yet
        fake = FakeSouthair([pnr_info(), pnr_info(), ISSUED])  # verify: not issued; issue: before, after
        assert recover(fake) == {reference: "issued"}
        assert recover(FakeSouthair([])) == {}  # once only
    assert fake.calls[0] == "pnr_info" and sum(1 for call in fake.calls if call[0] == "ticketing") == 1
    assert admin_statuses(reference) == ("ticket_issued", "paid", "issued")


def test_restart_after_the_supplier_ticketed_records_the_ticket_without_sending_again(website_hold):
    connection, reference = website_hold
    with patch.object(settings, "southair_ticketing_enabled", True):
        _paid_by_card(reference, "TRV-F-DONE")
        connection.execute(sa.text("UPDATE bookings SET issuance_status = 'processing' WHERE reference = :r"), {"r": reference})
        fake = FakeSouthair([ISSUED])
        assert recover(fake) == {reference: "issued"}
    assert fake.calls == ["pnr_info"]  # no ticketing call
    assert admin_statuses(reference) == ("ticket_issued", "paid", "issued")


def test_lost_background_task_is_retried_once_then_left_to_staff(website_hold):
    """Paid, not issued, nothing in progress (the task never ran, or failed for a passing reason): one retry, never two."""
    connection, reference = website_hold
    with patch.object(settings, "southair_ticketing_enabled", True):
        _paid_by_card(reference, "TRV-F-LOST")
        failing = FakeSouthair([pnr_info(), pnr_info(), pnr_info()], ticketing=SouthairError("ticketing_failed", "x"))
        assert recover(failing) == {reference: "needs_staff"}
        assert recover(FakeSouthair([pnr_info(), ISSUED])) == {}  # the one retry is used
    assert sum(1 for call in failing.calls if call[0] == "ticketing") == 1
    assert admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
    assert [a["kind"] for a in red_alerts(connection, reference)] == ["paid_not_issued"]
    actions = connection.scalars(sa.text("SELECT e.action FROM booking_events e JOIN bookings b ON b.id = e.booking_id"
                                         " WHERE b.reference = :r AND e.action = 'auto_issue_recovery'"), {"r": reference}).all()
    assert actions == ["auto_issue_recovery"]


def test_recovery_after_the_hold_ended_alerts_staff_and_never_issues(website_hold):
    connection, reference = website_hold
    with patch.object(settings, "southair_ticketing_enabled", True):
        _paid_by_card(reference, "TRV-F-LAPSED")
        fake = FakeSouthair([])
        assert recover(fake, at=datetime.now(timezone.utc) + timedelta(hours=4)) == {reference: "paid_not_ticketed"}
    assert fake.calls == []
    assert admin_statuses(reference) == ("paid_not_ticketed", "paid", "not_issued")
    assert red_alerts(connection, reference)[-1]["kind"] == "paid_hold_lost"


@pytest.mark.parametrize("method", ["bank_transfer", "jazzcash"])
def test_recovery_never_issues_bank_transfer_or_jazzcash(website_hold, method):
    connection, reference = website_hold
    account = payment_accounts.add(method, "Travelian receiving account")
    booking_payments.record(reference, amount_minor=TOTAL, currency="PKR", method=method, account_id=account.id, payer_reference="TXN-1",
                            received_at=datetime.now(timezone.utc), actor_user_id=None)
    fake = FakeSouthair([])
    with patch.object(settings, "southair_ticketing_enabled", True):
        assert recover(fake) == {}
    assert fake.calls == [] and admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")


def test_recovery_leaves_ticketing_off_bookings_to_staff(website_hold):
    connection, reference = website_hold
    with patch.object(settings, "southair_ticketing_enabled", False):
        _paid_by_card(reference, "TRV-F-OFF2")
        assert recover(FakeSouthair([])) == {}
    assert connection.scalar(sa.text("SELECT count(*) FROM booking_events WHERE action = 'auto_issue_recovery'")) == 0


# ---------- group fares: one payment however many PayFast copies arrive ----------

def test_group_payment_by_notification_form_post_and_return_is_recorded_once(database):
    """The form-post notification fix makes PayFast's posted notification count. With the customer's return as well, the
    group payment is still recorded once."""
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.database.test_public_booking_db import _booking, _payments

    connection, _, _ = database
    booking_id, reference = _booking(connection)  # a held website group booking, Rs 13,997
    public_bookings.create_attempt(reference, "TRV-G-BOTH", 1399700, "PKR")
    params = signed("TRV-G-BOTH", transaction_id="T77", transaction_amount="13997.00")
    client = TestClient(app)
    public_booking.PAY_LIMIT._hits.clear()
    started = []
    with patch.object(settings, "payfast_merchant_id", SecretStr(MERCHANT)), patch.object(settings, "payfast_secured_key", SecretStr(KEY)), \
         patch.object(flight_payments, "start_after_verified_card_payment", started.append):
        notify = client.post("/v1/public/payments/payfast/notify", data=params)  # form post, as PayFast sends it
        back = client.post("/v1/public/payments/payfast/return", json=params)
        again = client.post("/v1/public/payments/payfast/notify", data=params)
    assert notify.json() == {"status": "ok"} and back.json() == {"reference": reference, "result": "already_paid"}
    assert again.json() == {"status": "ok"}
    assert _payments(connection, booking_id) == [("payfast", 1399700, "PayFast", "T77")]
    assert connection.scalar(sa.text("SELECT payment_status FROM bookings WHERE id = :b"), {"b": booking_id}) == "paid"
    assert started == []  # group fares are issued by staff


def flight_fallback_alerts(at):
    from app.services import flight_fallback

    return flight_fallback.thirty_minute_alerts(at)


def test_second_red_alert_when_about_30_minutes_of_hold_are_left(website_hold):
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-30M", TOTAL, "PKR")
    with patch.object(settings, "southair_ticketing_enabled", False):
        pay(reference, "TRV-F-30M", signed("TRV-F-30M", transaction_amount="467.00"), [])
        asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: FakeSouthair([pnr_info()])))
        hold_end = connection.scalar(sa.text("SELECT hold_expires_at FROM bookings WHERE reference = :r"), {"r": reference})
        assert asyncio.run(flight_fallback_alerts(hold_end - timedelta(minutes=45))) == []  # not yet
        assert asyncio.run(flight_fallback_alerts(hold_end - timedelta(minutes=25))) == [reference]
        assert asyncio.run(flight_fallback_alerts(hold_end - timedelta(minutes=10))) == []  # once only
    alerts = red_alerts(connection, reference)
    assert [a["kind"] for a in alerts] == ["paid_not_issued", "paid_not_issued_30m"]
    assert alerts[1]["title"].startswith(f"URGENT: Payment received, ticket NOT issued — {reference}")


def test_red_alert_email_is_ready_and_logs_when_skipped(website_hold, caplog):
    """Email goes through SES once it is set up; until then every skip is logged in words and the admin alert still exists."""
    from unittest.mock import MagicMock

    from app.services import flight_fallback

    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-MAIL", TOTAL, "PKR")
    pay(reference, "TRV-F-MAIL", signed("TRV-F-MAIL", transaction_id="PF-1", transaction_amount="467.00"), [])
    with patch.object(settings, "notify_email_enabled", False):
        asyncio.run(flight_fallback.send_red_alert("paid_not_issued", reference, reason="test", hold="active"))
    assert any("Red alert email not sent (skipped: NOTIFY_EMAIL_ENABLED is off)" in r.getMessage() for r in caplog.records)

    ses = MagicMock()
    rejected = type("ClientError", (Exception,), {})("rejected")
    rejected.response = {"Error": {"Code": "MessageRejected"}}
    ses.send_email.side_effect = [None, rejected]
    with patch.object(settings, "notify_email_enabled", True), patch.object(settings, "notify_emails", "ops@example.invalid"), \
         patch("boto3.client", return_value=ses):
        asyncio.run(flight_fallback.send_red_alert("paid_not_issued", reference, reason="test", hold="active"))
        asyncio.run(flight_fallback.send_red_alert("paid_not_issued", reference, reason="test", hold="active"))
    message = ses.send_email.call_args_list[0].kwargs["Message"]
    assert message["Subject"]["Data"] == f"URGENT: Payment received, ticket NOT issued — {reference}"
    body = message["Body"]["Text"]["Data"]
    for line in ("Amount received: Rs 467", "PayFast transaction ID: PF-1", "Supplier: Southair", "PNR: 63B55C", "Hold status: active",
                 "Hold expires:", "Passengers: MR Sample Passenger, MISS Baby Passenger", "Route:", "Travel date:", "Reason: test",
                 "Booked and payment received. Confirm with supplier, issue the ticket, and share it with the customer."):
        assert line in body, line
    assert any("MessageRejected" in r.getMessage() and "isn't verified" in r.getMessage() for r in caplog.records)


def test_staff_rehold_of_an_expired_booking_at_an_accepted_new_price(website_hold):
    """Staff re-hold (Re-hold at SooperFare) of an expired, paid booking at a new price they accepted: the booking is held
    again under the new PNR, its total is the new price and the payment status follows (paid -> part paid)."""
    from app.db import booking_payments, supplier_bookings

    connection, reference = website_hold
    booking_id = connection.scalar(sa.text("SELECT id FROM bookings WHERE reference = :r"), {"r": reference})
    connection.execute(sa.text("UPDATE bookings SET provider_id = 'sooperfare', supplier_ref = 'uuid-1', booking_status = 'expired',"
                               " payment_status = 'paid' WHERE id = :b"), {"b": booking_id})
    connection.execute(sa.text("INSERT INTO booking_payments (id, booking_id, amount_minor, currency, method, received_into, received_at,"
                               " recorded_at, reference) VALUES ('p1', :b, :t, 'PKR', 'bank_transfer', 'UBL', now(), now(), 'TRX1')"),
                       {"b": booking_id, "t": TOTAL})
    later = datetime.now(timezone.utc) + timedelta(hours=5)
    values = dict(provider="sooperfare", pnr="NEWPNR", supplier_ref="uuid-2", hold_expires_at=later, supplier_total_minor=45_000,
                  selling_total_minor=TOTAL + 5_000, supplier_response={"booking": "x"})
    with pytest.raises(ValueError):  # a new price nobody accepted
        supplier_bookings.replace_hold(reference, **values)
    token = supplier_bookings.STAFF_REHOLD.set((None, TOTAL + 5_000, "uuid-other"))  # the booking changed meanwhile
    try:
        with pytest.raises(ValueError):
            supplier_bookings.replace_hold(reference, **values)
    finally:
        supplier_bookings.STAFF_REHOLD.reset(token)
    assert supplier_bookings.claim_rehold(reference, "uuid-1") and not supplier_bookings.claim_rehold(reference, "uuid-1")
    assert not supplier_bookings.claim_rehold(reference, "uuid-other")
    supplier_bookings.release_rehold(reference)
    assert supplier_bookings.claim_rehold(reference, "uuid-1")
    supplier_bookings.release_rehold(reference)
    token = supplier_bookings.STAFF_REHOLD.set((None, TOTAL + 5_000, "uuid-1"))
    try:
        supplier_bookings.replace_hold(reference, **values)
    finally:
        supplier_bookings.STAFF_REHOLD.reset(token)
    row = connection.execute(sa.text("SELECT pnr, booking_status, total_minor, payment_status FROM bookings WHERE id = :b"), {"b": booking_id}).one()
    assert tuple(row) == ("NEWPNR", "held", TOTAL + 5_000, booking_payments.payment_status(TOTAL, TOTAL + 5_000))
    sellings = connection.scalars(sa.text("SELECT selling_minor FROM booking_travelers WHERE booking_id = :b"), {"b": booking_id}).all()
    assert sum(s or 0 for s in sellings) in (0, TOTAL + 5_000)
