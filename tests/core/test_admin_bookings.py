"""Admin Bookings list and detail: filters, cards, sample labelling and D23 field visibility."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes import admin_bookings as routes
from app.api.routes import admin_dashboard as dashboard_routes
from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, require_back_office, resolve_actions
from app.db import booking_people
from app.db import bookings as booking_store
from app.db.bookings import BookingRow, BookingsUnavailable

PKT = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=PKT)

SUPER_ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))
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


def client(monkeypatch, rows=ROWS, viewer=SUPER_ADMIN, people=None):
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
    monkeypatch.setattr(booking_people, "read", lambda reference: people.get(reference) if people else None)
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


def test_cards_follow_the_filters_and_a_card_filters_the_list(monkeypatch):
    api = client(monkeypatch)
    cards = api.get("/v1/admin/bookings").json()["cards"]
    assert cards == {
        "all_bookings": 8, "confirmed": 2, "on_hold": 2, "cancelled": 2, "unpaid": 3,
        "expiring_soon": 1, "expiring_window_hours": 2,
    }
    # Cards count the rows in the current filters (spec 7).
    held = api.get("/v1/admin/bookings", params={"booking_status": "held"}).json()["cards"]
    assert held["all_bookings"] == 3 and held["on_hold"] == 2 and held["cancelled"] == 0 and held["unpaid"] == 3
    agency = api.get("/v1/admin/bookings", params={"agency": "ag-2"}).json()["cards"]
    assert agency["all_bookings"] == 1 and agency["confirmed"] == 1
    # A card lists exactly the rows it counts, and doesn't change the other cards.
    ids = lambda card, **p: [i["booking_id"] for i in api.get("/v1/admin/bookings", params={"card": card, **p}).json()["items"]]
    assert ids("on_hold") == ["TRV-2", "TRV-3"]  # TRV-9 has no deadline
    assert ids("cancelled") == ["TRV-5", "TRV-8"]  # cancelled or expired
    assert ids("unpaid") == ["TRV-2", "TRV-3", "TRV-9"]
    assert ids("expiring_soon") == ["TRV-2"]
    assert ids("confirmed") == ["TRV-4", "TRV-6"]
    picked = api.get("/v1/admin/bookings", params={"card": "unpaid"}).json()
    assert picked["cards"] == cards and picked["total"] == cards["unpaid"]
    assert ids("unpaid", agency="ag-2") == []
    assert api.get("/v1/admin/bookings", params={"card": "everything"}).status_code == 422
    flights = api.get("/v1/admin/bookings", params={"product": "flight"}).json()["cards"]
    assert flights["all_bookings"] == 7 and flights["awaiting_issuance"] == 1 and flights["hold_deadlines"] == 2
    assert ids("awaiting_issuance", product="flight") == ["TRV-4"]
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


PEOPLE = booking_people.People(
    customer=booking_people.Customer("Test", "Customer", "92", "3001234567", "ops@example.invalid"),
    travelers=[booking_people.Traveler(position=0, passenger_type="ADT", title="MR", first_name="Test", last_name="Passenger", id_type="PSPT",
                                       document_country="PAK", nationality="PAK", document_expiry=date(2027, 1, 15), infant_of_position=None,
                                       date_of_birth_masked="1990-••-••", document_number_masked="••••••567")],
    events=[booking_people.AuditEvent("hold_created", "u-1", NOW, {"label": "UAT test data", "pnr": "PNR2"}, "super_admin")],
)
REVEALER = BackOfficeUser("u-5", "staff", FieldVisibility(supplier_names=False, cost_and_margin=False, reveal_identity=True))


def test_detail_shows_customer_masked_passengers_and_audit_when_saved(monkeypatch):
    body = client(monkeypatch, viewer=STAFF, people={"TRV-2": PEOPLE}).get("/v1/admin/bookings/TRV-2").json()
    assert body["customer"] == {"first_name": "Test", "last_name": "Customer", "phone": "+92 3001234567", "email": "ops@example.invalid"}
    assert body["passengers"][0]["document_number_masked"] == "••••••567" and body["passengers"][0]["date_of_birth_masked"] == "1990-••-••"
    assert [(e["action"], e["actor_role"]) for e in body["audit"]] == [("hold_created", "super_admin")]
    assert body["can_reveal_identity"] is False
    assert client(monkeypatch, viewer=REVEALER, people={"TRV-2": PEOPLE}).get("/v1/admin/bookings/TRV-2").json()["can_reveal_identity"] is True
    passenger = body["passengers"][0]
    assert passenger["document_expiry_masked"] == "2027-••-••" and "document_expiry" not in passenger
    assert passenger["document_validity"] == "expiring"  # 15 Jan 2027 is within 6 months of today (5 Nov 2026 in these tests)
    assert "2027-01-15" not in str(body)
    assert "passengers" not in body["sections_not_available"] and "audit" not in body["sections_not_available"]
    other = client(monkeypatch, viewer=STAFF, people={"TRV-2": PEOPLE}).get("/v1/admin/bookings/TRV-1").json()
    assert "passengers" in other["sections_not_available"] and "customer" not in other


def test_list_shows_the_customer_when_there_is_no_agency(monkeypatch):
    rows = [booking(1, agency_id=None, agency_name=None, customer_name="Test Customer"), booking(2)]
    items = client(monkeypatch, rows=rows).get("/v1/admin/bookings").json()["items"]
    assert {i["booking_id"]: i["customer_or_agency"] for i in items} == {"TRV-1": "Test Customer", "TRV-2": "Karachi Travels"}


@pytest.mark.parametrize("viewer,status", [(STAFF, 403), (STAFF_NAMES, 403), (STAFF_COST, 403), (REVEALER, 200), (SUPER_ADMIN, 200)])
def test_reveal_needs_the_permission_and_is_not_cached(monkeypatch, viewer, status):
    calls = []
    def reveal(reference, position, actor, reason):
        calls.append((reference, position, actor, reason))
        return {"date_of_birth": "1990-01-31", "document_number": "AB1234567"}
    monkeypatch.setattr(booking_people, "reveal", reveal)
    response = client(monkeypatch, viewer=viewer).post("/v1/admin/bookings/TRV-2/travelers/0/reveal", json={"reason": "Ticketing needs it"})
    assert response.status_code == status
    if status == 200:
        assert response.json() == {"date_of_birth": "1990-01-31", "document_number": "AB1234567"}
        assert response.headers["cache-control"] == "no-store" and calls == [("TRV-2", 0, viewer.user_id, "Ticketing needs it")]
    else:
        assert calls == [] and "AB1234567" not in response.text


def test_reveal_needs_a_reason_and_reports_a_missing_traveler(monkeypatch):
    api = client(monkeypatch, viewer=REVEALER)
    assert api.post("/v1/admin/bookings/TRV-2/travelers/0/reveal", json={"reason": ""}).status_code == 422
    assert api.post("/v1/admin/bookings/TRV-2/travelers/0/reveal", json={}).status_code == 422
    for error, status in ((booking_people.NotFound(), 404),):
        def reveal(*args, error=error):
            raise error
        monkeypatch.setattr(booking_people, "reveal", reveal)
        response = api.post("/v1/admin/bookings/TRV-2/travelers/0/reveal", json={"reason": "Ticketing needs it"})
        assert response.status_code == status


def test_agency_and_customer_roles_cannot_reach_the_reveal(monkeypatch):
    response = client(monkeypatch, viewer=None).post("/v1/admin/bookings/TRV-2/travelers/0/reveal", json={"reason": "Ticketing needs it"})
    assert response.status_code == 403


PAYER = BackOfficeUser("u-6", "staff", NONE, frozenset({"record_payments"}))
PAYMENT_BODY = {"amount": {"amount_minor": 20_000, "currency": "PKR"}, "method": "jazzcash", "account_id": "acct-00000001",
                "reference": "JC-778", "received_at": "2026-11-05T06:30:00+00:00"}


@pytest.mark.parametrize("viewer,status", [(STAFF, 403), (STAFF_COST, 403), (REVEALER, 403), (PAYER, 201), (SUPER_ADMIN, 201)])
def test_recording_a_payment_needs_the_permission(monkeypatch, viewer, status):
    from app.db import booking_payments
    calls = []
    def record(reference, **kwargs):
        calls.append((reference, kwargs))
        return booking_payments.Payment("p1", kwargs["amount_minor"], "PKR", kwargs["method"], "JazzCash business wallet", kwargs["payer_reference"],
                                        kwargs["received_at"], viewer.user_id, viewer.role, NOW), "part_paid"
    monkeypatch.setattr(booking_payments, "record", record)
    response = client(monkeypatch, viewer=viewer).post("/v1/admin/bookings/TRV-2/payments", json=PAYMENT_BODY)
    assert response.status_code == status
    if status == 201:
        assert response.json()["payment_status"] == "part_paid" and response.json()["payment"]["method"] == "jazzcash"
        assert calls[0][0] == "TRV-2" and calls[0][1]["actor_user_id"] == viewer.user_id
    else:
        assert calls == []


@pytest.mark.parametrize("change,status", [
    ({"method": "cash"}, 422), ({"amount": {"amount_minor": 0, "currency": "PKR"}}, 422), ({"reference": ""}, 422),
    ({"received_at": "2026-11-05T06:30:00"}, 422), ({"received_at": "2026-11-06T06:30:00+00:00"}, 422),  # no zone; future
])
def test_payment_validation(monkeypatch, change, status):
    assert client(monkeypatch, viewer=PAYER).post("/v1/admin/bookings/TRV-2/payments", json={**PAYMENT_BODY, **change}).status_code == status


def test_detail_says_whether_the_viewer_may_record_payments(monkeypatch):
    assert client(monkeypatch, viewer=STAFF).get("/v1/admin/bookings/TRV-2").json()["can_record_payments"] is False
    assert client(monkeypatch, viewer=PAYER).get("/v1/admin/bookings/TRV-2").json()["can_record_payments"] is True


def test_part_paid_counts_as_unpaid_on_the_card_and_filters_as_part_paid(monkeypatch):
    rows = [booking(1, payment_status="part_paid", status="held", ticket_status="not_issued"), booking(2)]
    body = client(monkeypatch, rows=rows).get("/v1/admin/bookings").json()
    assert body["cards"]["unpaid"] == 1
    assert [i["statuses"]["payment"] for i in body["items"]] == ["part_paid", "paid"]
    filtered = client(monkeypatch, rows=rows).get("/v1/admin/bookings", params={"payment_status": "part_paid"}).json()
    assert [i["booking_id"] for i in filtered["items"]] == ["TRV-1"]


ISSUER = BackOfficeUser("u-7", "staff", NONE, frozenset({"issue_tickets"}))
ISSUER_COST = BackOfficeUser("u-8", "staff", FieldVisibility(supplier_names=False, cost_and_margin=True), frozenset({"issue_tickets"}))


def issuance_target(**overrides):
    from app.db import issuance as store
    values = dict(id="b1", reference="TRV-2", pnr="PNR2", provider="southair", product="flight", booking_status="held",
                  issuance_status="not_issued", hold_expires_at=NOW + timedelta(hours=5), currency="PKR", total_minor=1_000_000,
                  supplier_cost_minor=900_000, supplier_ref="REF", paid_minor=1_000_000, traveler_types=[(0, "ADT")])
    values.update(overrides)
    return store.Target(**values)


def checklist(monkeypatch, viewer, target, balance="50000.00"):
    from unittest.mock import AsyncMock, MagicMock
    from app.core.config import settings
    from app.db import issuance as store
    monkeypatch.setattr(store, "load", lambda reference: target)
    supplier = MagicMock()
    supplier.profile = AsyncMock(return_value={"limit": {"cap": balance, "ccy": "PKR"}})
    monkeypatch.setattr(routes, "configured_client", lambda: supplier)
    monkeypatch.setattr(settings, "southair_ticketing_enabled", True)
    return client(monkeypatch, viewer=viewer).get("/v1/admin/bookings/TRV-2/issuance")


@pytest.mark.parametrize("viewer", [STAFF, STAFF_NAMES, ISSUER])
def test_checklist_has_no_supplier_fields_without_cost_permission(monkeypatch, viewer):
    response = checklist(monkeypatch, viewer, issuance_target())
    body = response.json()
    assert response.status_code == 200 and not ({"supplier_amount", "supplier_balance", "margin"} & set(body))
    assert "southair" not in response.text.lower() and "900000" not in response.text
    assert body["can_issue"] is (viewer is ISSUER) and body["full_payment_recorded"] is True and body["blockers"] == []


@pytest.mark.parametrize("viewer", [SUPER_ADMIN, ISSUER_COST])
def test_checklist_shows_supplier_amount_balance_and_margin_with_cost_permission(monkeypatch, viewer):
    body = checklist(monkeypatch, viewer, issuance_target()).json()
    assert body["supplier_amount"]["amount_minor"] == 900_000 and body["margin"]["amount_minor"] == 100_000
    assert body["supplier_balance"]["amount_minor"] == 5_000_000
    assert "southair" not in str(body).lower()


def test_checklist_blockers(monkeypatch):
    target = issuance_target(paid_minor=400_000, hold_expires_at=NOW + timedelta(seconds=30))
    body = checklist(monkeypatch, ISSUER, target).json()
    assert body["can_issue"] is False and body["can_request"] is False and body["amount_due"]["amount_minor"] == 600_000
    assert any("Full payment" in b for b in body["blockers"]) and any("expired" in b for b in body["blockers"])


@pytest.mark.parametrize("path,body", [("/issue", None), ("/verify", None), ("/resolve", {"outcome": "not_issued", "reason": "Supplier said so"})])
def test_issue_verify_and_resolve_need_the_permission(monkeypatch, path, body):
    from app.services import issuance
    monkeypatch.setattr(issuance.store, "load", lambda reference: issuance_target(issuance_status="unknown"))
    monkeypatch.setattr(issuance.store, "claim", lambda *a: (_ for _ in ()).throw(AssertionError("must not claim")))
    response = client(monkeypatch, viewer=STAFF).post(f"/v1/admin/bookings/TRV-2/issuance{path}", json=body)
    assert response.status_code == 403 and "issue tickets permission" in response.json()["detail"]


def test_resolve_needs_a_reason_and_one_ticket_per_traveler(monkeypatch):
    api = client(monkeypatch, viewer=ISSUER)
    assert api.post("/v1/admin/bookings/TRV-2/issuance/resolve", json={"outcome": "not_issued", "reason": ""}).status_code == 422
    assert api.post("/v1/admin/bookings/TRV-2/issuance/resolve", json={"outcome": "issued", "reason": "Confirmed",
                    "ticket_numbers": [{"position": 0, "ticket_number": "2147483647001"}, {"position": 0, "ticket_number": "2147483647002"}]}).status_code == 422


def test_issue_result_carries_no_supplier_fields(monkeypatch):
    from app.services import issuance
    async def issue(reference, viewer, client_factory=None):
        return issuance.Outcome("issued", "issued", "Ticket issued.", {0: "2147483647001"})
    monkeypatch.setattr(issuance, "issue", issue)
    response = client(monkeypatch, viewer=ISSUER).post("/v1/admin/bookings/TRV-2/issuance/issue")
    assert response.json() == {"result": "issued", "issuance_status": "issued", "message": "Ticket issued.",
                               "tickets": [{"position": 0, "ticket_number": "2147483647001"}]}
