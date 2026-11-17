"""The bookings reader against a real schema: agency name join and sample marking."""

from app.db import bookings as store


def test_reader_joins_agency_name_and_keeps_import_source(database):
    connection, metadata, _ = database
    connection.execute(metadata.tables["agencies"].insert().values(id="test-agency", name="Synthetic Agency", status="active"))
    table = metadata.tables["bookings"]
    connection.execute(table.insert().values(id="b-1", reference="TRV-T1", pnr="AB12CD", product="flight", provider_id="SyntheticAir",
             agency_id="test-agency", booking_status="held", currency="PKR", total_minor=1000, supplier_cost_minor=900))
    connection.execute(table.insert().values(id="b-2", reference="TRV-T2", product="hotel", booking_status="confirmed", currency="PKR",
             total_minor=500, import_source="legacy_test_csv"))
    rows = {row.booking_id: row for row in store.list_dashboard_bookings()}
    assert rows["TRV-T1"].agency_id == "test-agency" and rows["TRV-T1"].agency_name == "Synthetic Agency"
    assert rows["TRV-T1"].updated_at is not None and rows["TRV-T1"].import_source is None
    assert rows["TRV-T2"].agency_name is None and rows["TRV-T2"].import_source == "legacy_test_csv"
