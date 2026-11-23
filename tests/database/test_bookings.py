import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.db.bookings import list_dashboard_bookings


def insert(connection, **overrides):
    values = dict(
        id=str(uuid.uuid4()), reference=f"TRV-{uuid.uuid4().hex[:8]}", pnr="AB12CD", product="flight",
        provider_id="SooperFare", booking_status="held", payment_status="unpaid", issuance_status="not_issued",
        hold_expires_at=datetime(2026, 11, 5, 9, tzinfo=timezone.utc), currency="PKR", total_minor=1_000_000,
        supplier_cost_minor=900_000, import_source=None,  # required since migration 0021_selling_price
    )
    values.update(overrides)
    connection.execute(sa.text(
        "INSERT INTO bookings (id, reference, pnr, product, provider_id, booking_status, payment_status, "
        "issuance_status, hold_expires_at, currency, total_minor, supplier_cost_minor, import_source) VALUES "
        "(:id, :reference, :pnr, :product, :provider_id, :booking_status, :payment_status, :issuance_status, "
        ":hold_expires_at, :currency, :total_minor, :supplier_cost_minor, :import_source)"
    ), values)
    return values


def test_migration_creates_bookings_table(database):
    connection, metadata, _ = database
    columns = {column.name for column in metadata.tables["bookings"].columns}
    assert {"reference", "pnr", "product", "booking_status", "payment_status", "issuance_status",
            "currency", "total_minor", "supplier_cost_minor", "import_source", "agency_id"} <= columns
    assert not {"passport_number", "email", "phone", "travelers"} & columns


def test_repository_reads_dashboard_rows(database):
    connection, _, _ = database
    stored = insert(connection, currency="INR", total_minor=4_200_050)
    rows = list_dashboard_bookings()
    assert len(rows) == 1
    row = rows[0]
    assert row.booking_id == stored["reference"] and row.currency == "INR" and row.total_minor == 4_200_050
    assert row.status == "held" and row.payment_status == "unpaid" and row.created_at is not None


@pytest.mark.parametrize("overrides", [
    {"product": "visa"}, {"booking_status": "lost"}, {"payment_status": "pending"},
    {"currency": "pkr"}, {"total_minor": -1}, {"pnr": "ab 12"},
])
def test_constraints_reject_invalid_rows(database, overrides):
    connection, _, _ = database
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            insert(connection, **overrides)


def test_reference_is_unique(database):
    connection, _, _ = database
    insert(connection, reference="TRV-SAME")
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            insert(connection, reference="TRV-SAME")
