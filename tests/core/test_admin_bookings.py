"""Admin Bookings list and detail: filters, cards, sample labelling and D23 field visibility."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes import admin_bookings as routes
from app.api.routes import admin_dashboard as dashboard_routes
from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, require_back_office
from app.db import bookings as booking_store
from app.db.bookings import BookingRow, BookingsUnavailable

PKT = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=PKT)

SUPER_ADMIN = BackOfficeUser("u-1", "super_admin", ALL)
STAFF = BackOfficeUser("u-2", "staff", NONE)
STAFF_NAMES = BackOfficeUser("u-3", "staff", FieldVisibility(supplier_names=True, cost_and_margin=False))
STAFF_COST = BackOfficeUser("u-4", "staff", FieldVisibility(supplier_names=False, cost_and_margin=True))

SUPPLIER_KEYS = {"provider", "providers"}
COST_KEYS = {"supplier_cost", "margin", "supplier_payment"}


def booking(n, **overrides):
    values = dict(
        booking_id=f"TRV-{n}", pnr=f"PNR{n}", product="flight", provider="SooperFare", status="ticket_issued",
        payment_status="paid", ticket_status="issued", created_at=NOW - timedelta(hours=n),
        hold_expires_at=None, currency="PKR", total_minor=1_000_000, supplier_cost_minor=900_000,
        agency_id="ag-1", agency_name="Karachi Travels",
    )
    values.update(overrides)
    return BookingRow(**values)


ROWS = [
    booking(1),
    booking(2, status="held", payment_status="unpaid", ticket_status="not_issued", hold_expires_at=NOW + timedelta(minutes=90)),
    booking(3, status="held", payment_status="unpaid", ticket_status="not_issued", hold_expires_at=NOW + timedelta(hours=5)),
    booking(4, status="confirmed", ticket_status="not_issued", provider="AirSial", agency_id="ag-2", agency_name="Lahore Tours"),
    booking(5, status="cancelled", payment_status="unpaid", ticket_status="not_issued"),
    booking(8, status="expired", payment_status="unpaid", ticket_status="not_issued", hold_expires_at=NOW - timedelta(hours=1)),
    booking(9, status="held", payment_status="unpaid", ticket_status="not_issued"),  # hold without a deadline
    booking(6, product="hotel", status="confirmed", ticket_status=None, import_source="legacy_test_csv"),
    booking(7, product="tour"),  # retired product: never listed
]


def client(monkeypatch, rows=ROWS, viewer=SUPER_ADMIN):
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    if viewer is None:
        def deny():
            raise HTTPException(403, "Back-office access denied.")
        app.dependency_overrides[require_back_office] = deny
    else:
        app.dependency_overrides[require_back_office] = lambda: viewer

    def read():
        if rows is None:
            raise BookingsUnavailable()
        return rows

    monkeypatch.setattr(booking_store, "list_dashboard_bookings", read)
    monkeypatch.setattr(dashboard_routes, "_now", lambda: NOW)
    monkeypatch.setattr(routes, "_now", lambda: NOW)
    return TestClient(app)


def keys(value):
    """Every key anywhere in a JSON value."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


# ---------- list ----------

def test_list_carries_source_sample_label_and_newest_first(monkeypatch):
    body = client(monkeypatch).get("/v1/admin/bookings").json()
    assert body["source"] == "gateway_bookings"
    assert body["contains_sample_data"] is True and body["sample_label"] == "Sample data"
    assert [i["booking_id"] for i in body["items"]] == ["TRV-1", "TRV-2", "TRV-3", "TRV-4", "TRV-5", "TRV-6", "TRV-8", "TRV-9"]
    assert [i["sample"] for i in body["items"]] == [False] * 5 + [True] + [False] * 2
    assert body["items"][0]["amount"] == {"amount_minor": 1_000_000, "currency": "PKR"}
    assert body["items"][0]["customer_or_agency"] == "Karachi Travels"
    real = client(monkeypatch, rows=ROWS[:1]).get("/v1/admin/bookings").json()
    assert real["contains_sample_data"] is False


def test_cards_follow_the_product_tab_not_the_filters(monkeypatch):
    api = client(monkeypatch)
    cards = api.get("/v1/admin/bookings").json()["cards"]
    assert cards == {
        "all_bookings": 8, "confirmed": 2, "on_hold": 2, "cancelled": 2, "unpaid": 3,
        "expiring_soon": 1, "expiring_window_hours": 2,
    }
    assert api.get("/v1/admin/bookings", params={"booking_status": "held"}).json()["cards"] == cards
    flights = api.get("/v1/admin/bookings", params={"product": "flight"}).json()["cards"]
    assert flights["all_bookings"] == 7 and flights["awaiting_issuance"] == 1 and flights["hold_deadlines"] == 2
    hotels = api.get("/v1/admin/bookings", params={"product": "hotel"}).json()["cards"]
    assert hotels["all_bookings"] == 1 and "awaiting_issuance" not in hotels


def test_filters(monkeypatch):
    api = client(monkeypatch)
    ids = lambda **p: [i["booking_id"] for i in api.get("/v1/admin/bookings", params=p).json()["items"]]
    assert ids(q="pnr4") == ["TRV-4"]
    assert ids(q="trv-2") == ["TRV-2"]
    assert ids(booking_status="held") == ["TRV-2", "TRV-3", "TRV-9"]
    assert ids(payment_status="unpaid") == ["TRV-2", "TRV-3", "TRV-5", "TRV-8", "TRV-9"]
    assert ids(issuance_status="issued") == ["TRV-1"]
    assert ids(agency="ag-2") == ["TRV-4"]
    assert ids(provider="airsial") == ["TRV-4"]
    assert ids(period="today", date_type="created") == ["TRV-1", "TRV-2", "TRV-3", "TRV-4", "TRV-5", "TRV-6", "TRV-8", "TRV-9"]
    page = api.get("/v1/admin/bookings", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 8 and [i["booking_id"] for i in page["items"]] == ["TRV-3", "TRV-4"]


def test_list_validates_input(monkeypatch):
    api = client(monkeypatch)
    for params in ({"product": "tour"}, {"period": "forever"}, {"booking_status": "lost"},
                   {"date_type": "expiry"}, {"limit": 0}, {"limit": 201}, {"offset": -1}):
        assert api.get("/v1/admin/bookings", params=params).status_code == 422, params


def test_filter_options(monkeypatch):
    options = client(monkeypatch).get("/v1/admin/bookings").json()["filter_options"]
    assert options["agencies"] == [{"value": "ag-1", "label": "Karachi Travels"}, {"value": "ag-2", "label": "Lahore Tours"}]
    assert options["providers"] == [{"value": "airsial", "label": "AirSial"}, {"value": "sooperfare", "label": "SooperFare"}]


# ---------- detail ----------

def test_detail_for_super_admin_has_fare_cost_margin_and_supplier_payment(monkeypatch):
    body = client(monkeypatch).get("/v1/admin/bookings/TRV-2").json()
    assert body["source"] == "gateway_bookings" and body["sample"] is False and "sample_label" not in body
    assert body["statuses"] == {"booking": "held", "payment": "unpaid", "issuance": "not_issued"}
    assert body["provider"] == "SooperFare"
    assert body["fare"] == {
        "billable_price": {"amount_minor": 1_000_000, "currency": "PKR"},
        "supplier_cost": {"amount_minor": 900_000, "currency": "PKR"},
        "margin": {"amount_minor": 100_000, "currency": "PKR"},
    }
    assert body["supplier_payment"]["amount_due"] == {"amount_minor": 900_000, "currency": "PKR"}
    assert body["deadline"] == body["supplier_payment"]["hold_deadline"]
    assert body["sections_not_available"] == ["itinerary", "passengers", "payments", "tickets", "servicing", "audit"]


def test_detail_sample_label_404_and_retired(monkeypatch):
    api = client(monkeypatch)
    sample = api.get("/v1/admin/bookings/TRV-6").json()
    assert sample["sample"] is True and sample["sample_label"] == "Sample data"
    assert api.get("/v1/admin/bookings/NOPE").status_code == 404
    assert api.get("/v1/admin/bookings/TRV-7").status_code == 404  # retired product


def test_unknown_cost_is_omitted_not_zero(monkeypatch):
    body = client(monkeypatch, rows=[booking(1, supplier_cost_minor=None)]).get("/v1/admin/bookings/TRV-1").json()
    assert "supplier_cost" not in body["fare"] and "margin" not in body["fare"]
    assert "amount_due" not in body["supplier_payment"]


# ---------- D23 visibility ----------

def test_plain_staff_never_receive_supplier_cost_or_margin(monkeypatch):
    api = client(monkeypatch, viewer=STAFF)
    listing = api.get("/v1/admin/bookings").json()
    detail = api.get("/v1/admin/bookings/TRV-2").json()
    for body in (listing, detail):
        assert not keys(body) & (SUPPLIER_KEYS | COST_KEYS)
    raw = api.get("/v1/admin/bookings").text + api.get("/v1/admin/bookings/TRV-4").text
    assert "SooperFare" not in raw and "AirSial" not in raw and "900000" not in raw
    assert api.get("/v1/admin/bookings", params={"provider": "airsial"}).status_code == 403


def test_grants_are_independent(monkeypatch):
    names = client(monkeypatch, viewer=STAFF_NAMES)
    body = names.get("/v1/admin/bookings/TRV-2").json()
    assert body["provider"] == "SooperFare" and not keys(body) & COST_KEYS
    assert "providers" in names.get("/v1/admin/bookings").json()["filter_options"]
    assert names.get("/v1/admin/bookings", params={"provider": "airsial"}).status_code == 200

    cost = client(monkeypatch, viewer=STAFF_COST)
    body = cost.get("/v1/admin/bookings/TRV-2").json()
    assert "provider" not in body and body["fare"]["margin"]["amount_minor"] == 100_000
    assert "supplier_payment" in body
    assert not keys(cost.get("/v1/admin/bookings").json()) & SUPPLIER_KEYS


@pytest.mark.parametrize("path", ["/v1/admin/bookings", "/v1/admin/bookings/TRV-1"])
def test_agency_and_customer_roles_are_refused_and_reads_fail_closed(monkeypatch, path):
    assert client(monkeypatch, viewer=None).get(path).status_code == 403
    assert client(monkeypatch, rows=None).get(path).status_code == 503
