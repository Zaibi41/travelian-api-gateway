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


def test_a_website_flight_request_is_marked_and_shown_to_the_guest_without_the_group_loader(database):
    """The 500 of 19 Nov: a website flight request was saved, then its reply used the group-only loader. The real save,
    mark and guest view now work for a flight request, with no PNR, nothing payable and the travelers' names."""
    import asyncio
    from app.db import public_bookings as pb, supplier_bookings
    from app.services import public_booking as service
    from tests.database.test_booking_people import CUSTOMER, TRAVELERS

    connection, metadata, _ = database
    reference = supplier_bookings.save_request(
        provider="sooperfare", currency="PKR", selling_total_minor=2207000, supplier_total_minor=2207000, actor_user_id=None,
        customer=CUSTOMER, travelers=TRAVELERS, channel="website",
        itinerary=[{"origin": "LHE", "destination": "KHI", "flight_number": "PA401", "airline_name": "Airblue",
                    "departure_local": "2026-11-27T11:15", "arrival_local": "2026-11-27T13:00"}], details={"restricted_airline": False})
    token, token_hash = pb.new_token()
    pb.mark_website_reference(reference, token_hash)
    assert pb.guest_can_open(reference, token)
    view = asyncio.run(service.booking_view(reference))
    assert view.status == "requested" and view.live is False and view.can_pay is False and view.final_pnr is None
    assert view.total.amount_minor == 2207000 and view.legs[0].flight_number == "PA401" and view.legs[0].departure_time == "11:15"
    assert view.travelers and all(name for name in view.travelers)



def _customer(connection, booking_id, email="ali@example.invalid", code="+92", phone="3001234567"):
    connection.execute(sa.text(
        "INSERT INTO booking_customers (booking_id, first_name, last_name, phone_code, phone, email)"
        " VALUES (:b, 'Ali', 'Khan', :pc, :p, :e)"), {"b": booking_id, "pc": code, "p": phone, "e": email})


def test_contact_change_saves_audits_masked_and_keeps_the_hold(database):
    """travelian#56: contact details change on the booking, an audit row (who, when, masked old -> new); hold and total untouched."""
    from app.services import booking_contact

    connection, _, _ = database
    booking_id, reference = _booking(connection)
    _customer(connection, booking_id)
    changed = booking_contact.change(reference, email="new@example.invalid", phone_code="+971", phone="501234567", actor_user_id=None, website=True)
    assert changed.contact == booking_contact.Contact(email="new@example.invalid", phone_code="+971", phone="501234567")
    assert (changed.fields, changed.old_email, changed.booking_id) == (("email", "phone"), "ali@example.invalid", booking_id)
    row = connection.execute(sa.text("SELECT email, phone_code, phone FROM booking_customers WHERE booking_id = :b"), {"b": booking_id}).one()
    assert tuple(row) == ("new@example.invalid", "+971", "501234567")
    booking = connection.execute(sa.text("SELECT booking_status, total_minor FROM bookings WHERE id = :b"), {"b": booking_id}).one()
    assert tuple(booking) == ("held", 1399700)
    audit = connection.execute(sa.text("SELECT actor_user_id, details, created_at FROM booking_events WHERE booking_id = :b AND action = 'customer_contact_changed'"),
                               {"b": booking_id}).one()
    assert audit.actor_user_id is None and audit.created_at is not None
    assert audit.details == {"via": "website", "by": "customer (booking access token)",
                             "email": {"from": "a***@example.invalid", "to": "n***@example.invalid"}, "phone": {"from": "+92 ***567", "to": "+971 ***567"}}
    again = booking_contact.change(reference, email=None, phone_code="+971", phone="501234567", actor_user_id=None, website=True)
    assert again.fields == ()
    assert connection.scalar(sa.text("SELECT COUNT(*) FROM booking_events WHERE booking_id = :b AND action = 'customer_contact_changed'"), {"b": booking_id}) == 1


def test_contact_change_online_only_on_open_unpaid_website_bookings(database):
    from app.services import booking_contact

    connection, _, _ = database
    cases = [("held", "part_paid", "website"), ("held", "paid", "website"), ("cancelled", "unpaid", "website"),
             ("expired", "unpaid", "website"), ("held", "unpaid", "staff")]
    for status, payment, channel in cases:
        booking_id, reference = _booking(connection, status=status, channel=channel)
        _customer(connection, booking_id)
        connection.execute(sa.text("UPDATE bookings SET payment_status = :p WHERE id = :b"), {"p": payment, "b": booking_id})
        try:
            booking_contact.change(reference, email=None, phone_code="+92", phone="3007654321", actor_user_id=None, website=True)
            raise AssertionError(f"expected a refusal for {status}/{payment}/{channel}")
        except booking_contact.ContactRefused as error:
            assert (error.status, error.code) == (409, "closed")
        staff = booking_contact.change(reference, email=None, phone_code="+92", phone="3007654321", actor_user_id=None, website=False)
        assert staff.contact.phone == "3007654321"


def test_alfalah_payment_recorded_once_and_flagged_as_sandbox_money(database):
    """Migration 0036: a Bank Alfalah attempt records method 'alfalah' once; a PayFast result can't settle it (nor the reverse)."""
    connection, _, _ = database
    booking_id, reference = _booking(connection)
    public_bookings.create_attempt(reference, "TRVGABCDEF12345678", 1399700, "PKR", provider="alfalah")
    wrong_provider = public_bookings.record_result("TRVGABCDEF12345678", paid=True, err_code="000", transaction_id="T1",
                                                   amount_minor=1399700, message=None, source="return", now=NOW)
    first = public_bookings.record_result("TRVGABCDEF12345678", paid=True, err_code="00", transaction_id="1263781929",
                                          amount_minor=1399700, message=None, source="return", now=NOW, provider="alfalah", test_money=True)
    again = public_bookings.record_result("TRVGABCDEF12345678", paid=True, err_code="00", transaction_id="1263781929",
                                          amount_minor=1399700, message=None, source="notification", now=NOW, provider="alfalah")
    assert wrong_provider.status == "unknown_basket"
    assert (first.status, first.payment_status, again.status) == ("paid", "paid", "already_paid")
    assert _payments(connection, booking_id) == [("alfalah", 1399700, "Bank Alfalah", "1263781929")]
    assert public_bookings.attempt_by_basket("TRVGABCDEF12345678").reference == reference
    review = connection.scalar(sa.text("SELECT details -> 'needs_review' FROM booking_events WHERE booking_id = :b AND action = 'payment_recorded'"),
                               {"b": booking_id})
    assert "sandbox_test_money" in review


def test_sandbox_paid_flight_is_never_picked_for_automatic_issuing(database):
    """flight_recovery retries stuck card-paid flights; a booking paid with test money (APG sandbox) is never one of them."""
    from app.db import flight_recovery, issuance

    connection, _, _ = database
    booking_id, reference = _booking(connection)
    connection.execute(sa.text("UPDATE bookings SET product = 'flight', updated_at = :t WHERE id = :b"), {"t": NOW - timedelta(hours=1), "b": booking_id})
    public_bookings.create_attempt(reference, "TRVGSANDBOX1234", 1399700, "PKR", provider="alfalah", test_money=True)
    public_bookings.record_result("TRVGSANDBOX1234", paid=True, err_code="00", transaction_id="1", amount_minor=1399700, message=None,
                                 source="return", now=NOW, provider="alfalah")
    connection.execute(sa.text("UPDATE bookings SET updated_at = :t WHERE id = :b"), {"t": NOW - timedelta(hours=1), "b": booking_id})
    assert reference not in [ref for ref, *_ in flight_recovery.stuck_card_payments(NOW)]
    assert issuance.load(reference).test_money is True


def test_payfast_works_before_migration_0036(database, monkeypatch):
    """Migrations run by hand after a deploy: with the new release on the old schema, a PayFast payment, the issuance check
    and the recovery job work as before (no provider / test_money columns yet)."""
    from alembic import command

    from app.db import flight_recovery, issuance, online_attempts

    connection, _, config = database
    command.downgrade(config, "0035_document_issue_date")
    monkeypatch.setattr(online_attempts, "_has_columns", False)
    booking_id, reference = _booking(connection)
    connection.execute(sa.text("UPDATE bookings SET product = 'flight' WHERE id = :b"), {"b": booking_id})
    public_bookings.create_attempt(reference, "B-OLD-SCHEMA", 1399700, "PKR")
    paid = public_bookings.record_result("B-OLD-SCHEMA", paid=True, err_code="000", transaction_id="T1", amount_minor=1399700,
                                         message=None, source="notification", now=NOW)
    assert (paid.status, paid.payment_status) == ("paid", "paid")
    assert _payments(connection, booking_id) == [("payfast", 1399700, "PayFast", "T1")]
    assert issuance.load(reference).test_money is False
    connection.execute(sa.text("UPDATE bookings SET updated_at = :t WHERE id = :b"), {"t": NOW - timedelta(hours=1), "b": booking_id})
    assert reference in [ref for ref, *_ in flight_recovery.stuck_card_payments(NOW)]
    assert online_attempts.has_alfalah_columns(connection) is False
