"""A hold's customer and travelers (migrations 0011, 0015): plain text in the sandbox phase,
returned in full to back-office readers (nothing is masked)."""

from datetime import date, datetime, timezone

import logging

import pytest
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.field_visibility import ALL, NONE, FieldVisibility
from app.db import booking_people
from app.db import bookings as store
from app.db import supplier_bookings
from app.db.supplier_bookings import CustomerRecord, TravelerRecord
from app.services import admin_bookings

ACTOR = "11111111-1111-4111-8111-111111111111"
CUSTOMER = CustomerRecord(first_name="Test", last_name="Customer", phone_code="92", phone="3001234567", email="ops@example.invalid")
# Fixture travelers are "Sample", not "Test": a passenger named TEST makes a test booking, which is never issued.
TRAVELERS = [
    TravelerRecord(passenger_type="ADT", title="MR", first_name="Sample", last_name="Passenger", date_of_birth=date(1990, 1, 31),
                   id_type="PSPT", document_number="AB1234567", document_country="PAK", nationality="PAK", document_expiry=date(2030, 5, 1)),
    TravelerRecord(passenger_type="INF", title="MISS", first_name="Baby", last_name="Passenger", date_of_birth=date(2025, 6, 1),
                   id_type="PSPT", infant_of_position=0),
]


# A hold that hasn't run out (a fixed date in the past made payments refuse "The hold has expired" once it passed).
HOLD_EXPIRES_AT = datetime(2036, 11, 20, 13, 18, tzinfo=timezone.utc)

def save(connection, metadata, **overrides):
    if not connection.execute(text("SELECT 1 FROM users WHERE id=:id"), {"id": ACTOR}).first():
        connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    args = dict(provider="southair", pnr="63B55C", supplier_ref="REF", hold_expires_at=HOLD_EXPIRES_AT,
                currency="PKR", selling_total_minor=46_700, supplier_total_minor=45_770, supplier_response={}, actor_user_id=ACTOR,
                audit={"pnr": "63B55C", "pax_count": 2}, customer=CUSTOMER, travelers=TRAVELERS)
    return supplier_bookings.save_hold(**{**args, **overrides})


ROLES = {"staff": NONE, "staff with names": FieldVisibility(True, False), "staff with cost": FieldVisibility(False, True), "super admin": ALL}


def test_stored_as_plain_text_without_encryption_columns(database):
    connection, metadata, _ = database
    save(connection, metadata)
    columns = {c["name"] for c in sa_inspect(connection).get_columns("booking_travelers")}
    assert {"date_of_birth", "document_number"} <= columns
    assert not columns & {"date_of_birth_encrypted", "date_of_birth_masked", "document_number_encrypted", "document_number_masked",
                          "document_number_hash", "key_id"}
    adult, infant = connection.execute(text("SELECT date_of_birth, document_number, infant_of_position FROM booking_travelers ORDER BY position")).all()
    assert (adult.date_of_birth, adult.document_number) == (date(1990, 1, 31), "AB1234567")
    assert (infant.document_number, infant.infant_of_position) == (None, 0)
    customer = connection.execute(text("SELECT first_name, last_name, phone_code, phone, email FROM booking_customers")).one()
    assert tuple(customer) == ("Test", "Customer", "92", "3001234567", "ops@example.invalid")


@pytest.mark.parametrize("role", list(ROLES))
def test_list_detail_passengers_and_audit_show_full_identity_for_every_role(database, role):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    row = admin_bookings.find_booking(store.list_dashboard_bookings(), reference)
    assert row.customer_name == "Test Customer"
    item = admin_bookings.build_list([row], admin_bookings.BookingListQuery(), datetime.now(timezone.utc), 24, ROLES[role]).items[0]
    assert item.customer_or_agency == "Test Customer"

    detail = admin_bookings.build_detail(row, ROLES[role], booking_people.read(reference), today=date(2026, 11, 9))
    body = detail.model_dump_json()
    assert "passengers" not in detail.sections_not_available and "audit" not in detail.sections_not_available
    assert [p.first_name for p in detail.passengers] == ["Sample", "Baby"]
    adult, infant = detail.passengers
    assert (adult.document_number, str(adult.date_of_birth)) == ("AB1234567", "1990-01-31")
    assert (str(adult.document_expiry), adult.document_validity) == ("2030-05-01", "valid")
    assert infant.document_number is None and str(infant.date_of_birth) == "2025-06-01" and infant.infant_of_position == 0
    assert "•" not in body
    assert [(e.action, e.details.get("travelers")) for e in detail.audit] == [("hold_created", 2)]
    assert detail.audit[0].actor_user_id == ACTOR


def test_an_old_reveal_identity_grant_row_is_still_accepted_by_the_database(database):
    connection, _, _ = database
    connection.execute(text("INSERT INTO role_permissions (role_id, permission) VALUES (4, 'reveal_identity')"))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(text("INSERT INTO role_permissions (role_id, permission) VALUES (4, 'see_everything')"))
