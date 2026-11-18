"""Migration 0022 and PayFast payment recording against PostgreSQL (rolled back after each test)."""

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.db import public_bookings

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)


def _booking(connection, status="held", total=1399700, channel="website"):
    booking_id = str(uuid.uuid4())
    reference = "TRV-G-" + booking_id[:6].upper()
    connection.execute(sa.text(
        "INSERT INTO bookings (id, reference, product, booking_status, payment_status, issuance_status, currency, total_minor,"
        " supplier_cost_minor, hold_expires_at, channel) VALUES (:id, :ref, 'group', :s, 'unpaid', 'not_issued', 'PKR', :t, :t, :exp, :ch)"),
        {"id": booking_id, "ref": reference, "s": status, "t": total, "exp": NOW + timedelta(hours=1), "ch": channel})
    return booking_id, reference


def _payments(connection, booking_id):
    return connection.execute(sa.text("SELECT method, amount_minor, received_into, reference FROM booking_payments WHERE booking_id = :b"),
                              {"b": booking_id}).all()


def test_requested_status_and_website_channel_allowed(database):
    connection, _, _ = database
    booking_id, _ = _booking(connection, status="requested")
    assert connection.scalar(sa.text("SELECT channel FROM bookings WHERE id = :b"), {"b": booking_id}) == "website"


def test_verified_payment_recorded_once(database):
    connection, _, _ = database
    booking_id, reference = _booking(connection)
    public_bookings.create_attempt(reference, "B-PAID-1", 1399700, "PKR")
    first = public_bookings.record_result("B-PAID-1", paid=True, err_code="000", transaction_id="T1", amount_minor=1399700,
                                          message=None, source="notification", now=NOW)
    again = public_bookings.record_result("B-PAID-1", paid=True, err_code="000", transaction_id="T1", amount_minor=1399700,
                                          message=None, source="return", now=NOW)
    assert (first.status, first.payment_status, again.status) == ("paid", "paid", "already_paid")
    assert _payments(connection, booking_id) == [("payfast", 1399700, "PayFast", "T1")]
    assert connection.scalar(sa.text("SELECT payment_status FROM bookings WHERE id = :b"), {"b": booking_id}) == "paid"
    actions = connection.scalars(sa.text("SELECT action FROM booking_events WHERE booking_id = :b ORDER BY created_at"), {"b": booking_id}).all()
    assert "online_payment_started" in actions and actions.count("payment_recorded") == 1


def test_wrong_amount_or_failure_records_nothing(database):
    connection, _, _ = database
    booking_id, reference = _booking(connection)
    public_bookings.create_attempt(reference, "B-WRONG", 1399700, "PKR")
    wrong = public_bookings.record_result("B-WRONG", paid=True, err_code="000", transaction_id="T2", amount_minor=100,
                                          message=None, source="return", now=NOW)
    public_bookings.create_attempt(reference, "B-FAIL", 1399700, "PKR")
    failed = public_bookings.record_result("B-FAIL", paid=False, err_code="002", transaction_id=None, amount_minor=None,
                                           message="Declined", source="return", now=NOW)
    unknown = public_bookings.record_result("B-NONE", paid=True, err_code="000", transaction_id=None, amount_minor=None,
                                            message=None, source="return", now=NOW)
    assert (wrong.status, failed.status, unknown.status) == ("amount_mismatch", "failed", "unknown_basket")
    assert _payments(connection, booking_id) == []
    assert connection.scalar(sa.text("SELECT status FROM payfast_attempts WHERE basket_id = 'B-FAIL'")) == "failed"


def test_payfast_method_allowed_but_unknown_methods_refused(database):
    connection, _, _ = database
    booking_id, _ = _booking(connection)
    insert = sa.text("INSERT INTO booking_payments (id, booking_id, amount_minor, currency, method, received_into, reference, received_at)"
                     " VALUES (:id, :b, 1, 'PKR', :m, 'x', :r, now())")
    connection.execute(insert, {"id": str(uuid.uuid4()), "b": booking_id, "m": "payfast", "r": "R1"})
    try:
        with connection.begin_nested():
            connection.execute(insert, {"id": str(uuid.uuid4()), "b": booking_id, "m": "bitcoin", "r": "R2"})
        refused = False
    except sa.exc.IntegrityError:
        refused = True
    assert refused


def test_forged_or_unverified_payfast_never_pays_or_issues(database):
    """B2: only a PayFast result the gateway verified (secured-key hash + amount) records a payment; issuing needs it."""
    import asyncio
    import hashlib
    from unittest.mock import patch

    from pydantic import SecretStr

    from app.core.config import settings
    from app.db import issuance
    from app.services import public_booking

    connection, _, _ = database
    booking_id, reference = _booking(connection)
    connection.execute(sa.text("UPDATE bookings SET pnr = 'ABC123', supplier_ref = 'REF', product = 'flight' WHERE id = :b"), {"b": booking_id})
    public_bookings.create_attempt(reference, "B-FORGE", 1399700, "PKR")
    with patch.object(settings, "payfast_merchant_id", SecretStr("14833")), patch.object(settings, "payfast_secured_key", SecretStr("key")):
        forged = asyncio.run(public_booking.payfast_result(
            {"basket_id": "B-FORGE", "err_code": "000", "transaction_amount": "13997.00", "validation_hash": "0" * 64}, "return"))
        wrong_key = hashlib.sha256(b"B-FORGE|not-the-key|14833|000").hexdigest()
        guessed = asyncio.run(public_booking.payfast_result({"basket_id": "B-FORGE", "err_code": "000", "validation_hash": wrong_key}, "notification"))
    assert forged is None and guessed is None
    assert _payments(connection, booking_id) == []
    assert connection.scalar(sa.text("SELECT payment_status FROM bookings WHERE id = :b"), {"b": booking_id}) == "unpaid"
    target = issuance.load(reference)
    assert "Full payment isn't recorded yet." in issuance.blockers(target, NOW, 60)
    assert connection.scalar(sa.text("SELECT issuance_status FROM bookings WHERE id = :b"), {"b": booking_id}) == "not_issued"


def test_lookup_needs_the_reference_and_the_contact_used_to_book(database):
    import asyncio

    from app.services import public_booking

    connection, _, _ = database
    booking_id, reference = _booking(connection)
    connection.execute(sa.text("INSERT INTO booking_customers (booking_id, first_name, last_name, phone_code, phone, email)"
                               " VALUES (:b, 'Ali', 'Khan', '92', '3001234567', 'Ali.Khan@Example.pk')"), {"b": booking_id})
    connection.execute(sa.text("INSERT INTO booking_travelers (id, booking_id, position, passenger_type, title, first_name, last_name, id_type,"
                               " date_of_birth, ticket_number, selling_minor, cost_minor) VALUES (:id, :b, 1, 'ADT', 'MR', 'Ali', 'Khan', 'PSPT', '1990-01-01', 'TKT-SECRET', 1399700, 1399700)"),
                       {"id": str(uuid.uuid4()), "b": booking_id})
    connection.execute(sa.text("UPDATE bookings SET hold_expires_at = now() + interval '2 hours' WHERE id = :b"), {"b": booking_id})
    for contact in ("ali.khan@example.pk", "+92 300 1234567", "03001234567"):
        assert public_bookings.lookup(reference, contact) is not None, contact
    for contact in ("other@example.pk", "03009999999"):
        assert public_bookings.lookup(reference, contact) is None, contact
    assert public_bookings.lookup("TRV-G-NOPE", "ali.khan@example.pk") is None
    with connection.begin_nested():
        pass
    view = asyncio.run(public_booking.lookup(reference.lower(), "03001234567"))
    assert view.status == "held" and view.travelers == ["MR Ali Khan"] and view.tickets == [] and view.airline_pnr is None
    assert "Pay before the hold time ends" in view.next_steps
