"""Supplier holds saved in `bookings` with an audit entry; supplier_ref and response stay server-side."""

from datetime import datetime, timezone

from sqlalchemy import text

from app.core.field_visibility import NONE
from app.db import bookings as store
from app.db import supplier_bookings
from app.services import admin_bookings


def test_hold_is_saved_as_uat_test_data_with_an_audit_entry(database):
    connection, metadata, _ = database
    connection.execute(metadata.tables["users"].insert().values(id="11111111-1111-4111-8111-111111111111", cognito_issuer="https://t.invalid", cognito_sub="s"))
    reference = supplier_bookings.save_hold(
        provider="southair", pnr="63B55C", supplier_ref="TICKET-REF-SECRET", hold_expires_at=datetime(2026, 11, 20, 13, 18, tzinfo=timezone.utc),
        currency="PKR", selling_total_minor=46_700, supplier_total_minor=45_770, supplier_response={"booking": {"pnrn": "63B55C"}},
        actor_user_id="11111111-1111-4111-8111-111111111111", audit={"pnr": "63B55C"},
    )
    row = connection.execute(text("SELECT booking_status, payment_status, issuance_status, import_source, supplier_ref FROM bookings WHERE reference=:r"), {"r": reference}).one()
    assert tuple(row) == ("held", "unpaid", "not_issued", "southair_uat", "TICKET-REF-SECRET")
    event = connection.execute(text("SELECT action, details->>'label' FROM booking_events")).one()
    assert tuple(event) == ("hold_created", "UAT test data")

    rows = store.list_dashboard_bookings()
    detail = admin_bookings.build_detail(admin_bookings.find_booking(rows, reference), NONE).model_dump_json()
    assert '"sample_label":"UAT test data"' in detail and "TICKET-REF-SECRET" not in detail and "45770" not in detail


def test_issuance_can_be_unknown(database):
    connection, metadata, _ = database
    reference = supplier_bookings.save_hold(
        provider="southair", pnr="ABC123", supplier_ref="x", hold_expires_at=None, currency="PKR", selling_total_minor=1, supplier_total_minor=1,
        supplier_response={}, actor_user_id=None, audit={},
    )
    connection.execute(text("UPDATE bookings SET issuance_status='unknown' WHERE reference=:r"), {"r": reference})
    item = admin_bookings.build_detail(admin_bookings.find_booking(store.list_dashboard_bookings(), reference), NONE)
    assert item.statuses.issuance == "unknown"


SEGMENT = {"direction": "outbound", "origin": "LHE", "destination": "KHI", "airline_code": "Z8", "airline_name": "South Air", "flight_number": "Z8266",
           "departure_local": "2026-11-19T00:25", "arrival_local": "2026-11-19T02:35", "duration_minutes": 130, "stops": 0, "stop_cities": [],
           "fare_name": "EB", "baggage": "20 kg", "meal": None}


def test_itinerary_is_saved_with_the_hold_and_shown_on_the_detail(database):
    reference = supplier_bookings.save_hold(
        provider="southair", pnr="ABC123", supplier_ref="x", hold_expires_at=None, currency="PKR", selling_total_minor=1, supplier_total_minor=1,
        supplier_response={}, actor_user_id=None, audit={}, itinerary=[SEGMENT],
    )
    detail = admin_bookings.build_detail(admin_bookings.find_booking(store.list_dashboard_bookings(), reference), NONE)
    assert "itinerary" not in detail.sections_not_available
    assert [(s.flight_number, s.origin, s.destination, s.departure_local, s.fare_name, s.baggage) for s in detail.itinerary] == [
        ("Z8266", "LHE", "KHI", "2026-11-19T00:25", "EB", "20 kg")]


def test_holds_without_an_itinerary_say_it_is_not_recorded(database):
    reference = supplier_bookings.save_hold(
        provider="southair", pnr="ABC124", supplier_ref="x", hold_expires_at=None, currency="PKR", selling_total_minor=1, supplier_total_minor=1,
        supplier_response={}, actor_user_id=None, audit={},
    )
    detail = admin_bookings.build_detail(admin_bookings.find_booking(store.list_dashboard_bookings(), reference), NONE)
    assert detail.itinerary is None and "itinerary" in detail.sections_not_available
