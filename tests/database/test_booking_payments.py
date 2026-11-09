"""Customer payments (migration 0013): amounts, statuses, duplicates, audit, and the Payments tab."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.core.field_visibility import NONE
from app.db import booking_payments, booking_people
from app.db import bookings as store
from app.services import admin_bookings
from tests.database.test_booking_people import ACTOR, save

AT = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)


def record(reference, amount, ref="TXN-1", method="bank_transfer", currency="PKR"):
    return booking_payments.record(reference, amount_minor=amount, currency=currency, method=method, received_into="Meezan Bank current account",
                                   payer_reference=ref, received_at=AT, actor_user_id=ACTOR)


@pytest.fixture
def booking(database):
    connection, metadata, _ = database
    reference = save(connection, metadata)  # total Rs 467
    return connection, reference


def test_part_then_full_payment_updates_status_and_writes_events(booking):
    connection, reference = booking
    _, status = record(reference, 20_000, "TXN-1")
    assert status == "part_paid"
    payment, status = record(reference, 26_700, "JC-778", method="jazzcash")
    assert status == "paid" and payment.method == "jazzcash" and payment.recorded_by_user_id == ACTOR
    assert connection.scalar(text("SELECT payment_status FROM bookings WHERE reference=:r"), {"r": reference}) == "paid"
    events = connection.execute(text("SELECT details FROM booking_events WHERE action='payment_recorded' ORDER BY created_at")).scalars().all()
    assert [(e["amount_minor"], e["payment_status"]) for e in events] == [(20_000, "part_paid"), (26_700, "paid")]

    row = admin_bookings.find_booking(store.list_dashboard_bookings(), reference)
    assert admin_bookings.payment_status(row) == "paid"
    detail = admin_bookings.build_detail(row, NONE, booking_people.read(reference))
    assert [p.amount.amount_minor for p in detail.payments] == [20_000, 26_700]
    assert detail.amount_paid.amount_minor == 46_700 and detail.amount_due.amount_minor == 0
    assert "payments" not in detail.sections_not_available
    assert [(e.action, e.details["method"]) for e in detail.audit if e.action == "payment_recorded"] == [
        ("payment_recorded", "bank_transfer"), ("payment_recorded", "jazzcash")]


@pytest.mark.parametrize("amount,ref,currency,status,message", [
    (46_701, "TXN-9", "PKR", 409, "more than the amount due"),
    (100, "TXN-1", "PKR", 409, "already recorded"),          # same method + reference (case-insensitive below)
    (100, "txn-1", "PKR", 409, "already recorded"),
    (100, "TXN-2", "USD", 422, "PKR"),
])
def test_refusals(booking, amount, ref, currency, status, message):
    _, reference = booking
    record(reference, 1_000, "TXN-1")
    with pytest.raises(booking_payments.PaymentRefused) as error:
        record(reference, amount, ref, currency=currency)
    assert error.value.status == status and message in error.value.message


def test_same_reference_with_another_method_is_allowed(booking):
    _, reference = booking
    record(reference, 1_000, "TXN-1", method="bank_transfer")
    _, status = record(reference, 1_000, "TXN-1", method="1link")
    assert status == "part_paid"


def test_closed_or_missing_booking_takes_no_payment(booking):
    connection, reference = booking
    connection.execute(text("UPDATE bookings SET booking_status='cancelled' WHERE reference=:r"), {"r": reference})
    with pytest.raises(booking_payments.PaymentRefused) as error:
        record(reference, 1_000)
    assert error.value.status == 409
    with pytest.raises(booking_payments.PaymentRefused) as error:
        record("TRV-NOPE00", 1_000)
    assert error.value.status == 404


def test_record_payments_is_a_grantable_permission(database):
    connection, _, _ = database
    connection.execute(text("INSERT INTO role_permissions (role_id, permission) VALUES (4, 'record_payments')"))
