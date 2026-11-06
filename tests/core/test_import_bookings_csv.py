import json

from app.cli.import_bookings_csv import map_row


def row(**overrides):
    values = {
        "pk": "BOOKING", "sk": "BOOKING#TRV-AB12CD", "id": "TRV-AB12CD", "bookingId": "TRV-AB12CD",
        "pnr": "ab12cd", "type": "flight", "status": "held", "paymentStatus": "pending", "ticketStatus": "not_issued",
        "createdAt": "2026-01-15T10:00:00.000Z", "holdExpiresAt": "2026-01-16T10:00:00.000Z",
        "updatedAt": "2026-01-15T11:00:00.000Z", "deletedAt": "",
        "pricing": json.dumps({"total": {"N": "42000.5"}, "currency": {"S": "inr"}, "base": {"N": "40000"}}),
        # Personal fields present in the export must never be copied.
        "passportNumber": "FAKE-PASSPORT-0000", "customerEmail": "someone@example.com", "contact": "{}",
    }
    values.update(overrides)
    return values


def test_maps_booking_fields_only_and_tags_the_source():
    record, reason, notes = map_row(row(), "legacy_test_csv")
    assert reason is None
    assert record["reference"] == "TRV-AB12CD" and record["pnr"] == "AB12CD"
    assert record["currency"] == "INR" and record["total_minor"] == 4_200_050
    assert record["payment_status"] == "unpaid" and "payment pending -> unpaid" in notes
    assert record["supplier_cost_minor"] is None and "no supplier cost" in notes
    assert record["import_source"] == "legacy_test_csv"
    assert not {"passportNumber", "customerEmail", "contact", "email"} & set(record)


def test_skips_rows_it_cannot_store_safely():
    assert map_row(row(deletedAt="2026-07-01T00:00:00Z"), "s")[1] == "deleted"
    assert map_row(row(type="visa"), "s")[1].startswith("product")
    assert map_row(row(status="weird"), "s")[1] == "unknown status"
    assert map_row(row(pricing="{}"), "s")[1] == "missing price or currency"
    assert map_row(row(createdAt=""), "s")[1] == "missing createdAt"
    record, _, notes = map_row(row(pnr="bad pnr!"), "s")
    assert record["pnr"] is None and "pnr dropped (invalid format)" in notes
