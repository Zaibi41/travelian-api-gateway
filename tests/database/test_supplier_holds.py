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
