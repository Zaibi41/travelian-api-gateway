"""Role-by-endpoint field check for /v1/group-ticketing/* (spec v3.0 D23).

Every endpoint is called through the real role resolution (require_search_viewer with
the user lookup and permission grants stubbed), so a role that should not see supplier
names, supplier cost, supplier-hosted logos or raw supplier fields is asserted clean.
"""

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import group_ticketing as routes
from app.core.authentication import require_authenticated_user
from app.core.booking_security import require_booking_identity
from app.core.config import settings
from app.core.field_visibility import SEE_COST_AND_MARGIN, SEE_SUPPLIER_NAMES
from app.services.group_ticketing import group_ticket_id
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups

SUPPLIER_LOGO = "https://cdn.supplier-host.example/uploads/airlines/7.png"
COST = "95000"
SUPPLIER_PNR = "SUPPNR9"
RAW_KEY = "ticket_airline_id"
PATH = "/v1/group-ticketing"


def groups():
    return normalize_groups([{
        "sector": "LHE - RUH", "airline": {"name": "Synthetic Air", "logo": SUPPLIER_LOGO},
        "tickets": [{
            "pnr": SUPPLIER_PNR, "date": "2026-12-01", "price": COST, "remaining": 9, "baggage": "20 KG",
            "subpart": [{"departure_time": "09:45", RAW_KEY: 7, "flight_number": "SY 101", "supplier_price": COST}],
        }],
    }])


TICKET_ID = group_ticket_id(groups()[0].tickets[0])

# role label -> (gateway role name, granted permissions)
ROLES = {
    "super_admin": ("super_admin", set()),
    "staff": ("staff", set()),
    "staff_names": ("staff", {SEE_SUPPLIER_NAMES}),
    "staff_cost": ("staff", {SEE_COST_AND_MARGIN}),
    "staff_both": ("staff", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN}),
    # Grants on these roles must be ignored.
    "agency_owner": ("agency_owner", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN}),
    "customer": ("customer", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN}),
    # Not gateway roles today; an unknown role must be refused, never widened.
    "admin": ("admin", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN}),
    "sub_agency": ("sub_agency", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN}),
}

READ_CALLS = {
    "inventory": ("GET", "/inventory", None),
    "search": ("POST", "/search", {"origin": "LHE", "destination": "RUH", "departure_date": "2026-12-01"}),
    "ticket": ("GET", f"/tickets/{TICKET_ID}", None),
    "check-availability": ("POST", "/check-availability", {"ticket_id": TICKET_ID, "adult": 1, "child": 0}),
    "flight-quotes": ("POST", "/flight-quotes", {"origin": "LHE", "destination": "RUH", "departure_date": "2026-12-01",
                                                 "passenger_count": 12, "contact_email": "ops@example.invalid"}),
}


def _client(monkeypatch, label):
    role, granted = ROLES[label]

    @contextmanager
    def transaction():
        yield object()

    monkeypatch.setattr("app.db.agencies.transaction", transaction)
    monkeypatch.setattr("app.db.users.ensure_user", lambda *_: SimpleNamespace(id="u", role=role, role_id=9))
    monkeypatch.setattr("app.core.field_visibility.granted_permissions", lambda *_: set(granted))
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    app.dependency_overrides[require_authenticated_user] = lambda: object()
    return TestClient(app)


def call(monkeypatch, label, endpoint, availability=True):
    method, path, body = READ_CALLS[endpoint]
    client = _client(monkeypatch, label)
    with (
        patch.object(routes, "fetch_all_group_inventory", new=AsyncMock(return_value=groups())),
        patch.object(routes, "_credentials_configured", return_value=True),
        patch.object(routes, "_check_dhillu_sahi_availability", new=AsyncMock(return_value=availability)),
        patch.object(settings, "group_ticketing_markup_percent", Decimal("5")),
        patch.object(settings, "group_ticketing_markup_fixed_minor", 0),
    ):
        return client.request(method, PATH + path, json=body)


def field_names(value, prefix=""):
    """Every key path in a JSON value, list items collapsed to []."""
    names = set()
    if isinstance(value, dict):
        for key, item in value.items():
            names.add(prefix + key)
            names |= field_names(item, prefix + key + ".")
    elif isinstance(value, list):
        for item in value:
            names |= field_names(item, prefix.rstrip(".") + "[].")
    return names


SUPPLIER_NAME_FIELDS = {"supplier", "pnr", "vendors_queried", "vendors_succeeded", "vendors_failed", "vendor_results"}


def leaks(response, may_see_names, may_see_cost):
    """What this response exposes that the caller may not see."""
    text = response.text
    names = {name.rsplit(".", 1)[-1].removesuffix("[]") for name in field_names(response.json())}
    found = []
    if not may_see_names:
        found += sorted(names & SUPPLIER_NAME_FIELDS)
        found += [marker for marker in ("dhillu", "skypass", SUPPLIER_PNR) if marker.lower() in text.lower()]
    if not may_see_cost:
        found += sorted(names & {"supplier_cost"})
        found += [marker for marker in (COST, "supplier_price") if marker in text]
    if not (may_see_names and may_see_cost):
        found += sorted(names & {"subparts", RAW_KEY})
    if "cdn.supplier-host" in text or "airline_logo" in names:
        found.append("supplier-hosted logo")
    return found


VISIBILITY = {  # label -> (supplier names, cost and margin)
    "super_admin": (True, True), "staff": (False, False), "staff_names": (True, False),
    "staff_cost": (False, True), "staff_both": (True, True), "agency_owner": (False, False),
}


@pytest.mark.parametrize("endpoint", list(READ_CALLS))
@pytest.mark.parametrize("label", list(VISIBILITY))
def test_each_role_gets_only_what_d23_allows(monkeypatch, label, endpoint):
    response = call(monkeypatch, label, endpoint)
    assert response.status_code in (200, 202), response.text
    assert leaks(response, *VISIBILITY[label]) == []


@pytest.mark.parametrize("endpoint", ["inventory", "search", "ticket"])
@pytest.mark.parametrize("label", ["super_admin", "staff", "agency_owner"])
def test_logos_are_always_gateway_paths(monkeypatch, label, endpoint):
    body = call(monkeypatch, label, endpoint).json()
    logos = [value for name, value in _walk(body) if name == "logo_url"]
    assert logos and all(value.startswith("/v1/logos/") for value in logos)


def _walk(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def test_permitted_roles_do_receive_supplier_fields(monkeypatch):
    ticket = call(monkeypatch, "super_admin", "ticket").json()
    assert {"supplier", "pnr", "supplier_cost", "subparts"} <= set(ticket)
    assert {"supplier", "pnr"} <= set(call(monkeypatch, "staff_names", "ticket").json())
    assert "supplier_cost" in call(monkeypatch, "staff_cost", "ticket").json()
    assert "vendors_queried" in call(monkeypatch, "staff_names", "check-availability").json()


@pytest.mark.parametrize("endpoint", list(READ_CALLS))
@pytest.mark.parametrize("label", ["customer", "admin", "sub_agency"])
def test_customer_and_unknown_roles_are_refused(monkeypatch, label, endpoint):
    response = call(monkeypatch, label, endpoint)
    assert response.status_code == 403
    assert response.json() == {"detail": "Search access denied."}


@pytest.mark.parametrize("path", ["/bookings", "/booking-details"])
def test_booking_endpoints_refuse_before_any_supplier_call(monkeypatch, path):
    """Booking is not wired: every caller gets 503 and no vendor fields, the supplier is never called."""
    monkeypatch.setattr("app.core.booking_security.authorize_agency_operation", lambda *_: object())
    monkeypatch.setattr("app.core.booking_security.require_active_assignment", lambda *_: None)
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    app.dependency_overrides[require_booking_identity] = lambda: object()
    body = {"booking_id": 1} if path == "/booking-details" else {}
    with patch.object(routes, "gather_vendor_calls", new=AsyncMock()) as supplier:
        response = TestClient(app).post(PATH + path, json=body)
    assert response.status_code in (422, 503)
    assert "vendor" not in response.text and "dhillu" not in response.text
    supplier.assert_not_called()
