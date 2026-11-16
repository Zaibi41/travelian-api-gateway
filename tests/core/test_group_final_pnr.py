"""Group bookings after payment (Paid – awaiting final PNR -> Final PNR received -> Ticketed) and the Flight Operations
list "Group departures without final PNR" (paid, no final PNR, departing within 48 hours, soonest first)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes import flight_operations
from app.core.field_visibility import NONE, BackOfficeUser, require_back_office
from app.db.bookings import BookingRow
from app.services.group_ticketing_status import LABEL, ticketing_status

PKT = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=PKT)


def test_statuses_after_payment():
    assert ticketing_status("held", "not_issued", None, paid_in_full=False) is None
    assert ticketing_status("held", "not_issued", None, paid_in_full=True) == "awaiting_final_pnr"
    assert ticketing_status("confirmed", "not_issued", "K9QX2L", paid_in_full=True) == "final_pnr_received"
    assert ticketing_status("ticket_issued", "issued", "K9QX2L", paid_in_full=True) == "ticketed"
    assert ticketing_status("ticket_issued", "issued", None, paid_in_full=True) == "awaiting_final_pnr"   # Ticketed needs the final PNR
    assert ticketing_status("cancelled", "not_issued", None, paid_in_full=True) is None
    assert [LABEL[s] for s in ("awaiting_final_pnr", "final_pnr_received", "ticketed")] == [
        "Paid – awaiting final PNR", "Final PNR received", "Ticketed"]


def row(n, leaves: str | None, **overrides):
    values = dict(booking_id=f"TRV-GRP-{n:06d}", pnr="SUPPLIER-PNR", product="group", provider="dhillu_sahi", status="held", payment_status="paid",
                  ticket_status="not_issued", created_at=NOW - timedelta(days=2), hold_expires_at=NOW + timedelta(hours=1), currency="PKR",
                  total_minor=1_000_000, supplier_cost_minor=900_000, customer_name="Ali Raza",
                  itinerary=[{"origin": "LHE", "destination": "JED", "airline_name": "Fly Jinnah", "flight_number": "9P742",
                              "departure_local": leaves}] if leaves else None)
    values.update(overrides)
    return BookingRow(**values)


ROWS = [
    row(1, "2026-11-17T22:00"),                                   # in 34h
    row(2, "2026-11-16T18:30"),                                   # in 6.5h: first
    row(3, "2026-11-18T12:30"),                                   # 48.5h: outside the window
    row(4, "2026-11-16T15:00", final_pnr="K9QX2L"),               # has its final PNR
    row(5, "2026-11-16T15:00", payment_status="part_paid"),       # not paid in full
    row(6, "2026-11-16T15:00", status="cancelled"),
    row(7, "2026-11-16T15:00", product="flight"),
    row(8, "2026-11-16T10:00"),                                   # already left
    row(9, None),                                                  # no flights recorded
]


def test_departures_list_paid_group_without_final_pnr_within_48h_soonest_first():
    result = flight_operations.group_departures_without_final_pnr(ROWS, NOW)
    assert result.window_hours == 48 and result.total == 2
    assert [item.booking_id for item in result.items] == ["TRV-GRP-000002", "TRV-GRP-000001"]
    first = result.items[0]
    assert first.departure_at == datetime(2026, 11, 16, 18, 30, tzinfo=PKT) and first.route == "LHE → JED"
    assert (first.airline_name, first.flight_number, first.customer_or_agency) == ("Fly Jinnah", "9P742", "Ali Raza")
    assert "SUPPLIER-PNR" not in result.model_dump_json() and "dhillu" not in result.model_dump_json()


def test_departures_route_is_back_office_only(monkeypatch):
    monkeypatch.setattr(flight_operations, "_read_bookings", lambda: ROWS)
    monkeypatch.setattr(flight_operations, "_now", lambda: NOW)
    app = FastAPI()
    app.include_router(flight_operations.router, prefix="/v1")
    app.dependency_overrides[require_back_office] = lambda: BackOfficeUser("u-1", "staff", NONE)
    body = TestClient(app).get("/v1/admin/flight-operations/group-departures-without-final-pnr").json()
    assert [item["booking_id"] for item in body["items"]] == ["TRV-GRP-000002", "TRV-GRP-000001"]

    def deny():
        raise HTTPException(403, "Back-office access denied.")
    app.dependency_overrides[require_back_office] = deny
    assert TestClient(app).get("/v1/admin/flight-operations/group-departures-without-final-pnr").status_code == 403
