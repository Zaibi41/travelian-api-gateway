"""Flight hold expiry on real PostgreSQL (skipped without TRAVELIAN_TEST_DATABASE_URL): held flights past their time
limit and not issued become expired; no supplier is called; payments already recorded stay and are reported."""

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db import booking_payments, flight_expiry, payment_accounts

ACTOR = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def flight(connection, n, *, status="held", issuance="not_issued", expires=NOW - timedelta(hours=1), paid=0):
    booking_id = f"00000000-0000-4000-8000-{n:012d}"
    connection.execute(sa.text("INSERT INTO bookings (id, reference, pnr, product, provider_id, booking_status, payment_status, issuance_status,"
                               " hold_expires_at, currency, total_minor, supplier_cost_minor)"
                               " VALUES (:id, :r, :p, 'flight', 'southair', :s, :pay, :i, :e, 'PKR', 2878000, 2800000)"),
                       {"id": booking_id, "r": f"TRV-F{n:05d}", "p": f"PNR{n:03d}", "s": status, "pay": "paid" if paid else "unpaid", "i": issuance, "e": expires})
    if paid:
        account = payment_accounts.add("bank_transfer", f"Meezan Bank, account {n}")
        connection.execute(sa.text("INSERT INTO booking_payments (id, booking_id, amount_minor, currency, method, received_into, reference, received_at,"
                                   " recorded_by_user_id, account_id) VALUES (:id, :b, :a, 'PKR', 'bank_transfer', 'Meezan', :ref, :at, :u, :acc)"),
                           {"id": f"11111111-0000-4000-8000-{n:012d}", "b": booking_id, "a": paid, "ref": f"TXN-{n}", "at": NOW, "u": ACTOR, "acc": account.id})
    return f"TRV-F{n:05d}"


def status(connection, reference):
    return connection.scalar(sa.text("SELECT booking_status FROM bookings WHERE reference = :r"), {"r": reference})


@pytest.fixture
def db(database):
    connection, metadata, _ = database
    connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    return connection


def test_unpaid_hold_becomes_expired_and_a_paid_one_paid_not_ticketed(db):
    """A paid flight whose hold ends has no ticket: 'paid_not_ticketed' (refund or rebook by hand), not plain 'expired'."""
    unpaid = flight(db, 1)
    paid = flight(db, 2, paid=2_878_000, status="pending_payment")
    assert flight_expiry.count_due(NOW) == 2
    expired = {item.reference: (item.paid_minor, item.status) for item in flight_expiry.expire_due(NOW)}
    assert expired == {unpaid: (0, "expired"), paid: (2_878_000, "paid_not_ticketed")}
    assert (status(db, unpaid), status(db, paid)) == ("expired", "paid_not_ticketed")
    action = db.scalar(sa.text("SELECT supplier_response -> 'action_needed' FROM bookings WHERE reference = :r"), {"r": paid})
    assert action["reason"] == "hold_expired" and action["amount_minor"] == 2_878_000
    assert db.scalar(sa.text("SELECT count(*) FROM booking_payments")) == 1  # the payment stays as it is
    details = db.execute(sa.text("SELECT e.details FROM booking_events e JOIN bookings b ON b.id = e.booking_id WHERE b.reference = :r"
                                 " AND e.action = 'hold_expired'"), {"r": paid}).scalar_one()
    assert details["reason_code"] == "payment_recorded_refund_by_hand" and details["amount_minor"] == 2_878_000


@pytest.mark.parametrize("kind", ["issued", "cancelled", "expired", "not_due", "processing", "unknown"])
def test_issued_closed_not_due_or_issuing_are_left_alone(db, kind):
    reference = {
        "issued": lambda: flight(db, 3, status="ticket_issued", issuance="issued"),
        "cancelled": lambda: flight(db, 4, status="cancelled"),
        "expired": lambda: flight(db, 5, status="expired"),
        "not_due": lambda: flight(db, 6, expires=NOW + timedelta(minutes=1)),
        "processing": lambda: flight(db, 7, issuance="processing"),
        "unknown": lambda: flight(db, 8, issuance="unknown"),
    }[kind]()
    before = status(db, reference)
    assert flight_expiry.expire_due(NOW) == [] and status(db, reference) == before


def test_a_second_run_changes_nothing(db):
    reference = flight(db, 9)
    assert [item.reference for item in flight_expiry.expire_due(NOW)] == [reference]
    revision = db.scalar(sa.text("SELECT revision FROM bookings WHERE reference = :r"), {"r": reference})
    assert flight_expiry.expire_due(NOW) == [] and flight_expiry.count_due(NOW) == 0
    assert db.scalar(sa.text("SELECT revision FROM bookings WHERE reference = :r"), {"r": reference}) == revision


def test_payment_refused_on_a_lapsed_flight_hold_before_the_job_and_after(db):
    reference = flight(db, 10, expires=datetime.now(timezone.utc) - timedelta(minutes=1))
    account = payment_accounts.add("bank_transfer", "Meezan Bank, current")
    pay = lambda: booking_payments.record(reference, amount_minor=100, currency="PKR", method="bank_transfer", account_id=account.id,  # noqa: E731
                                          payer_reference="TXN-X", received_at=datetime.now(timezone.utc), actor_user_id=ACTOR)
    with pytest.raises(booking_payments.PaymentRefused, match="expired"):
        pay()
    flight_expiry.expire_due(datetime.now(timezone.utc))
    with pytest.raises(booking_payments.PaymentRefused):
        pay()
