"""Issue #17 against a real bookings table: which existing bookings block a second booking of the same passenger."""

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from types import SimpleNamespace

from app.core.field_visibility import ALL, NONE, BackOfficeUser
from app.db import duplicate_bookings as store
from app.db import supplier_bookings
from app.db.group_holds import create_pending
from app.services import duplicate_bookings as rule

SEGMENT = {"direction": "outbound", "origin": "KHI", "destination": "ISB", "departure_local": "2026-12-10T09:00", "flight_number": "PK300"}
KHI_ISB = {("KHI", "ISB", "2026-12-10")}
ADMIN = BackOfficeUser("u1", "super_admin", ALL)
STAFF = BackOfficeUser("u2", "staff", NONE)
WEBSITE = SimpleNamespace(role="website", user_id="website", visibility=NONE)
CUSTOMER = supplier_bookings.CustomerRecord(first_name="Ali", last_name="Raza", phone_code="92", phone="3001234567", email="a@example.invalid")


def traveler(first="Ali", last="Raza", kind="ADT", dob=date(1988, 3, 14), document="AB1234567"):
    return supplier_bookings.TravelerRecord(passenger_type=kind, title="MR", first_name=first, last_name=last, date_of_birth=dob,
                                            id_type="PSPT", document_number=document)


def held(travelers=None, expires=None, pnr="ABC123"):
    return supplier_bookings.save_hold(
        provider="southair", pnr=pnr, supplier_ref="x", hold_expires_at=expires or datetime.now(timezone.utc) + timedelta(hours=2),
        currency="PKR", selling_total_minor=10_000, supplier_total_minor=9_000, supplier_response={}, actor_user_id=None, audit={},
        customer=CUSTOMER, travelers=travelers or [traveler()], itinerary=[SEGMENT])


def refused(passengers=None, journeys=KHI_ISB, exclude=None):
    try:
        asyncio.run(rule.check(passengers or [traveler(first=" ALI", last="raza ")], journeys, viewer=ADMIN, exclude_reference=exclude))
    except rule.DuplicateBooking as error:
        return error
    return None


def status(connection, reference, booking_status, payment_status="unpaid"):
    connection.execute(text("UPDATE bookings SET booking_status = :s, payment_status = :p WHERE reference = :r"),
                       {"s": booking_status, "p": payment_status, "r": reference})


def test_an_open_hold_blocks_the_same_passenger_on_the_same_route_and_day(database):
    reference = held()
    error = refused()
    assert error is not None and error.status == 409 and error.reference == reference
    assert refused(journeys={("KHI", "ISB", "2026-12-11")}) is None
    assert refused(journeys={("KHI", "LHE", "2026-12-10")}) is None
    assert refused(passengers=[traveler(first="Sara")]) is None
    assert refused(exclude=reference) is None  # a re-hold of this booking


@pytest.mark.parametrize("booking_status,payment_status,blocks", [
    ("held", "unpaid", True), ("pending_payment", "unpaid", True), ("payment_received", "paid", True), ("confirmed", "paid", True),
    ("ticket_issued", "paid", True), ("paid_not_ticketed", "paid", True), ("hold_unrecorded", "unpaid", True),
    ("cancelled", "unpaid", False), ("expired", "unpaid", False), ("hold_failed", "unpaid", False), ("requested", "unpaid", False),
])
def test_only_active_bookings_block(database, booking_status, payment_status, blocks):
    connection, _, _ = database
    status(connection, held(), booking_status, payment_status)
    assert (refused() is not None) is blocks


def test_an_unpaid_hold_past_its_time_limit_does_not_block_but_a_paid_one_does(database):
    connection, _, _ = database
    reference = held(expires=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert refused() is None
    status(connection, reference, "held", "paid")
    assert refused() is not None


def test_a_group_hold_being_placed_blocks_until_it_is_stale(database):
    connection, _, _ = database
    _, reference = create_pending(provider="skypass", currency="PKR", selling_total_minor=1, supplier_total_minor=1, supplier_response={},
                                  actor_user_id=None, customer=CUSTOMER, travelers=[traveler()],
                                  itinerary=[{"direction": "outbound", "origin": "KHI", "destination": "ISB", "departure_local": "2026-12-10"}],
                                  reference_prefix="TRV-GRP-")
    assert refused().reference == reference  # a flight for a passenger already on a group booking: refused
    connection.execute(text("UPDATE bookings SET created_at = now() - interval '2 hours' WHERE reference = :r"), {"r": reference})
    assert refused() is None


def test_children_and_infants_and_names_with_extra_spaces(database):
    held(travelers=[traveler(), traveler(first="Baby  Noor", kind="INF", dob=date(2026, 2, 1), document=None)])
    assert refused(passengers=[traveler(first="baby noor", kind="INF", dob=date(2026, 2, 1), document=None)]) is not None
    assert refused(passengers=[traveler(first="Baby Noor", kind="INF", dob=date(2025, 2, 1), document=None)]) is None
    assert store.travelers_named(["babynoorraza"])[0].first_name == "Baby  Noor"


def check(viewer, contact=None):
    return asyncio.run(rule.check([traveler()], KHI_ISB, viewer=viewer, contact=contact))


def test_an_unpaid_website_hold_warns_staff_and_a_paid_one_refuses(database):
    connection, _, _ = database
    reference = held()
    connection.execute(text("UPDATE bookings SET channel = 'website' WHERE reference = :r"), {"r": reference})
    warnings = check(STAFF)
    assert len(warnings) == 1 and reference in warnings[0] and warnings[0].startswith("Possible double booking")
    status(connection, reference, "payment_received", "paid")
    with pytest.raises(rule.DuplicateBooking):
        check(STAFF)


def test_the_website_is_refused_only_by_the_same_contacts_booking(database):
    held()  # contact a@example.invalid, 92 3001234567
    stranger = SimpleNamespace(email="someone@example.invalid", phone_code="92", phone="3330000000")
    assert check(WEBSITE, stranger) == []
    by_email = SimpleNamespace(email="A@example.invalid", phone_code="92", phone="3330000000")
    by_phone = SimpleNamespace(email="other@example.invalid", phone_code="+92", phone="300-1234567")
    for contact in (by_email, by_phone):
        with pytest.raises(rule.DuplicateBooking) as error:
            check(WEBSITE, contact)
        assert error.value.message == rule.WEBSITE_MESSAGE and error.value.reference is None


def test_an_unrecorded_hold_is_named_only_to_viewers_with_supplier_access(database):
    connection, _, _ = database
    reference = held()
    status(connection, reference, "hold_unrecorded")
    with pytest.raises(rule.DuplicateBooking) as staff:
        check(STAFF)
    assert reference not in staff.value.message and staff.value.reference is None
    with pytest.raises(rule.DuplicateBooking) as admin:
        check(ADMIN)
    assert admin.value.reference == reference
