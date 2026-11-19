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


def admin_statuses(reference):
    """What the admin booking detail shows (booking, payment, issuance)."""
    row = admin_bookings.find_booking(dashboard.list_dashboard_bookings(), reference)
    statuses = admin_bookings.build_detail(row, ALL, booking_people.read(reference), today=date.today()).statuses
    return statuses.booking, statuses.payment, statuses.issuance


def guest(reference):
    return asyncio.run(public_booking.booking_view(reference))


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
        assert admin_statuses(reference) == ("held", "paid", "not_issued")
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


def test_card_ticketing_off_is_paid_issuance_pending_and_nothing_is_sent(website_hold):
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-OFF", TOTAL, "PKR")
    fake = FakeSouthair([])
    with patch.object(settings, "southair_ticketing_enabled", False):
        assert pay(reference, "TRV-F-OFF", signed("TRV-F-OFF", transaction_amount="467.00"), []).status == "paid"
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "pending"
        view = guest(reference)
    assert fake.calls == []
    assert admin_statuses(reference) == ("held", "paid", "not_issued")
    assert (view.status, view.final_pnr, view.tickets) == ("confirmed", None, [])  # payment received, issuance pending
    assert notifications(connection, reference)[-1] == "paid_issuance_pending"


def test_card_supplier_failure_is_never_shown_as_issued(website_hold):
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-FAIL", TOTAL, "PKR")
    fake = FakeSouthair([pnr_info(), pnr_info()], ticketing=SouthairError("ticketing_failed", "x"))
    with patch.object(settings, "southair_ticketing_enabled", True):
        assert pay(reference, "TRV-F-FAIL", signed("TRV-F-FAIL", transaction_amount="467.00"), []).status == "paid"
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "failed"
        view = guest(reference)
    assert admin_statuses(reference) == ("held", "paid", "not_issued")
    assert (view.status, view.final_pnr, view.tickets) == ("processing", None, [])  # issuance in progress; no made-up PNR
    assert connection.scalars(sa.text("SELECT ticket_number FROM booking_travelers t JOIN bookings b ON b.id = t.booking_id"
                                      " WHERE b.reference = :r"), {"r": reference}).all() == [None, None]
    assert notifications(connection, reference)[-1] == "auto_issue_failed"


def test_card_insufficient_supplier_balance_refuses_before_sending(website_hold):
    """Southair UAT has a zero balance: the gateway refuses before any ticketing call, and staff are alerted."""
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-BAL", TOTAL, "PKR")
    fake = FakeSouthair([pnr_info()], balance="0.00")
    with patch.object(settings, "southair_ticketing_enabled", True):
        pay(reference, "TRV-F-BAL", signed("TRV-F-BAL", transaction_amount="467.00"), [])
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: fake)) == "failed"
    assert not any(call[0] == "ticketing" for call in fake.calls)
    assert admin_statuses(reference) == ("held", "paid", "not_issued")


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
        assert admin_statuses(reference) == ("held", "paid", "not_issued")
        assert guest(reference).status == "processing"
        fake = FakeSouthair([pnr_info(), ISSUED])
        assert asyncio.run(issuance.issue(reference, APPROVER, client_factory=lambda: fake)).result == "issued"  # the staff click
    assert admin_statuses(reference) == ("ticket_issued", "paid", "issued")
    assert guest(reference).final_pnr == "63B55C"
