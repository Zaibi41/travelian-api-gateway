"""A hold's customer and travelers (migration 0011): encrypted at rest, masked on read, logged reveal."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import pii
from app.core.field_visibility import NONE
from app.db import booking_people
from app.db import bookings as store
from app.db import supplier_bookings
from app.db.supplier_bookings import CustomerRecord, TravelerRecord
from app.services import admin_bookings
from tests.pii_keys import new_key, pii_test_keys

ACTOR = "11111111-1111-4111-8111-111111111111"
CUSTOMER = CustomerRecord(first_name="Test", last_name="Customer", phone_code="92", phone="3001234567", email="ops@example.invalid")
TRAVELERS = [
    TravelerRecord(passenger_type="ADT", title="MR", first_name="Test", last_name="Passenger", date_of_birth=date(1990, 1, 31),
                   id_type="PSPT", document_number="AB1234567", document_country="PAK", nationality="PAK", document_expiry=date(2030, 5, 1)),
    TravelerRecord(passenger_type="INF", title="MISS", first_name="Baby", last_name="Passenger", date_of_birth=date(2025, 6, 1),
                   id_type="PSPT", infant_of_position=0),
]


def save(connection, metadata, **overrides):
    if not connection.execute(text("SELECT 1 FROM users WHERE id=:id"), {"id": ACTOR}).first():
        connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    args = dict(provider="southair", pnr="63B55C", supplier_ref="REF", hold_expires_at=datetime(2026, 11, 20, 13, 18, tzinfo=timezone.utc),
                currency="PKR", selling_total_minor=46_700, supplier_total_minor=45_770, supplier_response={}, actor_user_id=ACTOR,
                audit={"pnr": "63B55C", "pax_count": 2}, customer=CUSTOMER, travelers=TRAVELERS)
    return supplier_bookings.save_hold(**{**args, **overrides})


def test_identity_is_encrypted_at_rest_with_masked_copy_hash_and_key_id(database):
    connection, metadata, _ = database
    with pii_test_keys():
        save(connection, metadata)
    rows = connection.execute(text("SELECT * FROM booking_travelers ORDER BY position")).mappings().all()
    dump = str([dict(r) for r in rows])
    assert "AB1234567" not in dump and "1990-01-31" not in dump and "2025-06-01" not in dump
    adult, infant = rows
    assert adult["date_of_birth_masked"] == "1990-••-••" and adult["document_number_masked"] == "••••••567"
    assert len(adult["document_number_hash"]) == 64 and adult["key_id"] == "k1"
    assert infant["document_number_encrypted"] is None and infant["infant_of_position"] == 0
    customer = connection.execute(text("SELECT first_name, last_name, phone_code, phone, email FROM booking_customers")).one()
    assert tuple(customer) == ("Test", "Customer", "92", "3001234567", "ops@example.invalid")


def test_without_keys_nothing_is_written(database):
    connection, metadata, _ = database
    with pytest.raises(pii.PiiNotConfigured):
        save(connection, metadata)
    assert connection.scalar(text("SELECT count(*) FROM bookings")) == 0


def test_list_detail_passengers_and_audit_read_the_saved_people(database):
    connection, metadata, _ = database
    with pii_test_keys():
        reference = save(connection, metadata)
    row = admin_bookings.find_booking(store.list_dashboard_bookings(), reference)
    assert row.customer_name == "Test Customer"
    item = admin_bookings.build_list([row], admin_bookings.BookingListQuery(), datetime.now(timezone.utc), 24, NONE).items[0]
    assert item.customer_or_agency == "Test Customer"

    detail = admin_bookings.build_detail(row, NONE, booking_people.read(reference))
    body = detail.model_dump_json()
    assert "passengers" not in detail.sections_not_available and "audit" not in detail.sections_not_available
    assert [p.first_name for p in detail.passengers] == ["Test", "Baby"]
    assert detail.passengers[0].document_number_masked == "••••••567" and detail.passengers[1].infant_of_position == 0
    assert detail.customer.phone == "+92 3001234567"
    assert "AB1234567" not in body and "1990-01-31" not in body
    assert [(e.action, e.details.get("travelers")) for e in detail.audit] == [("hold_created", 2)]


def test_reveal_returns_full_values_and_logs_who_which_and_why(database):
    connection, metadata, _ = database
    with pii_test_keys():
        reference = save(connection, metadata)
        values = booking_people.reveal(reference, 0, ACTOR, "Ticketing needs the passport number")
        assert values == {"date_of_birth": "1990-01-31", "document_number": "AB1234567"}
        assert booking_people.reveal(reference, 1, ACTOR, "Infant date of birth for the airline") == {"date_of_birth": "2025-06-01", "document_number": None}
        with pytest.raises(booking_people.NotFound):
            booking_people.reveal(reference, 5, ACTOR, "missing traveler")
        with pytest.raises(booking_people.NotFound):
            booking_people.reveal("TRV-NOPE00", 0, ACTOR, "missing booking")
    events = connection.execute(text("SELECT action, actor_user_id, details FROM booking_events WHERE action='identity_revealed' ORDER BY created_at")).all()
    assert [(e.action, e.actor_user_id, e.details["traveler_position"], e.details["fields"]) for e in events] == [
        ("identity_revealed", ACTOR, 0, ["date_of_birth", "document_number"]), ("identity_revealed", ACTOR, 1, ["date_of_birth"])]
    assert all("AB1234567" not in str(e.details) and "1990" not in str(e.details) for e in events)


def test_reveal_after_key_rotation_and_with_a_missing_key(database):
    connection, metadata, _ = database
    old = new_key()
    with pii_test_keys({"k1": old}):
        reference = save(connection, metadata)
    with pii_test_keys({"k1": old, "k2": new_key()}, active="k2"):
        assert booking_people.reveal(reference, 0, ACTOR, "after rotation")["document_number"] == "AB1234567"
    with pii_test_keys({"k2": new_key()}, active="k2"), pytest.raises(pii.PiiDecryptError):
        booking_people.reveal(reference, 0, ACTOR, "old key removed")


def test_reveal_identity_is_a_grantable_permission(database):
    connection, _, _ = database
    connection.execute(text("INSERT INTO role_permissions (role_id, permission) VALUES (4, 'reveal_identity')"))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(text("INSERT INTO role_permissions (role_id, permission) VALUES (4, 'see_everything')"))
