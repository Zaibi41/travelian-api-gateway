"""Group Ticketing field visibility (spec v3.0 D23), selling price and gateway-served logos."""

import asyncio
import json
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes.group_ticketing import router
from app.api.routes.logos import router as logos_router
from app.core import field_visibility
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, require_search_viewer
from app.services import supplier_logos
from app.services.group_ticketing_view import selling_price, supplier_cost
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups

SUPPLIER_LOGO = "https://cdn.supplier-name.example/public/uploads/airlines/1.png"


def groups():
    return normalize_groups([{
        "sector": "LHE - RUH", "airline": {"name": "Synthetic Air", "logo": SUPPLIER_LOGO},
        "tickets": [{
            "pnr": "SUPPNR1", "date": "2026-12-01", "price": "95000", "updated_at": "2026-05-08T05:39:41Z",
            "remaining": 9, "baggage": "20 KG",
            "subpart": [{"departure_time": "09:45", "ticket_airline_id": 7, "flight_number": "SY 101", "supplier_price": "95000"}],
        }],
    }])


VIEWERS = {
    "super_admin": BackOfficeUser("u1", "super_admin", ALL),
    "staff": BackOfficeUser("u2", "staff", NONE),
    "staff_names": BackOfficeUser("u3", "staff", FieldVisibility(supplier_names=True, cost_and_margin=False)),
    "staff_cost": BackOfficeUser("u4", "staff", FieldVisibility(supplier_names=False, cost_and_margin=True)),
    "agency_owner": BackOfficeUser("u5", "agency_owner", NONE),
}


def call(viewer, path="/v1/group-ticketing/inventory", markup=Decimal("5"), fixed=0):
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: viewer
    with (
        patch("app.api.routes.group_ticketing.fetch_all_group_inventory", new=AsyncMock(return_value=groups())),
        patch.object(settings, "group_ticketing_markup_percent", markup),
        patch.object(settings, "group_ticketing_markup_fixed_minor", fixed),
    ):
        client = TestClient(app)
        if path.endswith("/search"):
            return client.post(path, json={"origin": "LHE", "destination": "RUH", "departure_date": "2026-12-01"})
        return client.get(path)


def ticket_of(response):
    return response.json()["data"][0]["tickets"][0]


@pytest.mark.parametrize("role", ["staff", "agency_owner"])
@pytest.mark.parametrize("path", ["/v1/group-ticketing/inventory", "/v1/group-ticketing/search"])
def test_staff_and_agency_get_no_supplier_cost_or_raw_fields(role, path):
    response = call(VIEWERS[role], path)
    assert response.status_code == 200
    # The stock label's value "supplier" is for everyone; only supplier fields (keys) and supplier values must be absent.
    raw = response.text.replace('"stock":"supplier"', "")
    for leak in ("supplier", "SUPPNR1", "subparts", "ticket_airline_id", "airline_logo", "95000", "9500000", "cdn.", "dhillu", "skypass"):
        assert leak not in raw, leak
    ticket = ticket_of(response)
    assert ticket["selling_price"] == {"amount_minor": 9_975_000, "currency": "PKR"}  # 95,000 + 5% = 99,750
    assert ticket["logo_url"].startswith("/v1/logos/") and ticket["departure_time"] == "09:45"
    assert set(ticket) <= {"id", "updated_at", "sold", "departure_date", "departure_time", "sector", "airline",
                           "logo_url", "remaining", "baggage", "meal", "ticket_type", "selling_price", "stock", "can_hold",
                           "legs", "flight_number", "hand_carry", "hold_unavailable_reason"}
    assert ticket["stock"] == "supplier" and ticket["can_hold"] is False  # Dhillu & Sahi can't hold
    assert ticket["hold_unavailable_reason"] == "Holding isn't available for this provider"


def test_super_admin_gets_supplier_cost_and_raw_fields():
    ticket = ticket_of(call(VIEWERS["super_admin"]))
    assert ticket["supplier"] == "dhillu_sahi" and ticket["pnr"] == "SUPPNR1"
    assert ticket["subparts"][0]["ticket_airline_id"] == 7
    assert ticket["supplier_cost"] == {"amount_minor": 9_500_000, "currency": "PKR"}
    assert "airline_logo" not in json.dumps(ticket) and "cdn." not in json.dumps(ticket)


def test_grants_are_independent():
    names = ticket_of(call(VIEWERS["staff_names"]))
    assert "supplier" in names and "subparts" not in names and "supplier_cost" not in names
    cost = ticket_of(call(VIEWERS["staff_cost"]))
    assert "supplier_cost" in cost and "supplier" not in cost and "pnr" not in cost


def test_single_ticket_follows_the_same_rules():
    listing = ticket_of(call(VIEWERS["staff"]))
    one = call(VIEWERS["staff"], f"/v1/group-ticketing/tickets/{listing['id']}")
    assert one.status_code == 200 and one.json() == listing


def test_no_markup_means_no_price_at_all():
    with patch.object(settings, "group_ticketing_markup_fixed_minor", None):
        ticket = ticket_of(call(VIEWERS["staff"], markup=None, fixed=None))
    assert "selling_price" not in ticket and "95000" not in json.dumps(ticket)
    assert ticket["can_hold"] is False


def test_a_markup_set_to_zero_on_purpose_sells_at_the_listing_price():
    ticket = ticket_of(call(VIEWERS["staff"], markup=Decimal("0")))
    assert ticket["selling_price"] == {"amount_minor": 9_500_000, "currency": "PKR"}


def test_selling_price_rounds_up_and_adds_fixed_amount():
    cost = supplier_cost(SimpleNamespace(price="1,000.50", currency=None))
    assert cost.amount_minor == 100_050 and cost.currency == "PKR"
    with patch.object(settings, "group_ticketing_markup_percent", Decimal("2.5")), \
         patch.object(settings, "group_ticketing_markup_fixed_minor", 50_000):
        assert selling_price(cost).amount_minor == 152_600  # 1000.50 * 1.025 + 500 = 1525.51 -> 1526
    for bad in ("", "abc", "0", "-5", None):
        assert supplier_cost(SimpleNamespace(price=bad, currency=None)) is None


# ---------- sign-in and roles ----------

def test_signed_out_callers_get_401(monkeypatch):
    monkeypatch.setattr(settings, "cognito_issuer", "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Test1")
    monkeypatch.setattr(settings, "cognito_app_client_id", "testclient123")
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    client = TestClient(app)
    assert client.get("/v1/group-ticketing/inventory").status_code == 401
    body = {"origin": "LHE", "destination": "RUH", "departure_date": "2026-12-01"}
    assert client.post("/v1/group-ticketing/search", json=body).status_code == 401
    assert client.post("/v1/group-ticketing/check-availability", json={"pnr": "X", "adult": 1, "child": 0, "price": 1}).status_code == 401


@pytest.mark.parametrize("role,allowed", [("super_admin", True), ("staff", True), ("agency_owner", True), ("customer", False)])
def test_customers_are_refused(monkeypatch, role, allowed):
    @contextmanager
    def transaction():
        yield object()

    profile = SimpleNamespace(id="u", role=role, role_id=1)
    monkeypatch.setattr("app.db.agencies.transaction", transaction)
    monkeypatch.setattr("app.db.users.ensure_user", lambda connection, identity: profile)
    monkeypatch.setattr(field_visibility, "granted_permissions", lambda *args: set(field_visibility.PERMISSIONS))
    if allowed:
        viewer = field_visibility._back_office_user(object(), field_visibility.SEARCH_ROLES, "denied")
        assert viewer.visibility == (ALL if role in ("super_admin", "staff") else NONE)  # agency: never, even with grant rows
    else:
        with pytest.raises(HTTPException) as error:
            field_visibility._back_office_user(object(), field_visibility.SEARCH_ROLES, "denied")
        assert error.value.status_code == 403


# ---------- logos ----------

class FakeStream:
    def __init__(self, status=200, content_type="image/png", body=b"\x89PNG"):
        self.status_code, self.headers, self.body = status, {"content-type": content_type}, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        yield self.body


def fake_client(stream, seen):
    class Client:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url):
            return stream

    return Client


@pytest.fixture(autouse=True)
def clear_logo_cache():
    supplier_logos._urls.clear()
    supplier_logos._images.clear()


def test_logo_paths_are_opaque_and_only_registered_ids_are_served(monkeypatch):
    path = supplier_logos.register(SUPPLIER_LOGO)
    assert path.startswith("/v1/logos/") and "supplier" not in path
    assert supplier_logos.register("http://insecure.example/x.png") is None
    assert supplier_logos.register("/relative.png") is None
    app = FastAPI()
    app.include_router(logos_router, prefix="/v1")
    assert TestClient(app).get("/v1/logos/" + "0" * 32).status_code == 404
    monkeypatch.setattr(supplier_logos, "_public_host", lambda host: True)
    seen = []
    monkeypatch.setattr(supplier_logos.httpx, "AsyncClient", fake_client(FakeStream(), seen))
    response = TestClient(app).get(path)
    assert response.status_code == 200 and response.content == b"\x89PNG"
    assert response.headers["content-type"] == "image/png" and response.headers["x-content-type-options"] == "nosniff"
    assert seen[0]["follow_redirects"] is False


@pytest.mark.parametrize("stream,public", [
    (FakeStream(content_type="image/svg+xml"), True),
    (FakeStream(content_type="text/html"), True),
    (FakeStream(status=302), True),
    (FakeStream(body=b"x" * (supplier_logos.MAX_BYTES + 1)), True),
    (FakeStream(), False),
])
def test_logo_fetch_refuses_unsafe_responses(stream, public):
    logo_id = supplier_logos.register(SUPPLIER_LOGO).rsplit("/", 1)[1]
    with patch.object(supplier_logos, "_public_host", lambda host: public):
        with pytest.raises(supplier_logos.LogoUnavailable):
            asyncio.run(supplier_logos.fetch(logo_id, client_factory=fake_client(stream, [])))


def test_private_addresses_are_not_public():
    assert supplier_logos._public_host("localhost") is False


@pytest.mark.parametrize("role", ["staff", "staff_cost", "agency_owner"])
@pytest.mark.parametrize("outcome", ["success", "failure", "unconfigured"])
def test_availability_omits_supplier_metadata(role, outcome):
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: VIEWERS[role]
    supplier = AsyncMock(return_value=True)
    if outcome == "failure":
        supplier.side_effect = RuntimeError("synthetic upstream failure")
    with (
        patch("app.api.routes.group_ticketing._credentials_configured", return_value=outcome != "unconfigured"),
        patch("app.api.routes.group_ticketing._check_dhillu_sahi_availability", supplier),
    ):
        response = TestClient(app).post("/v1/group-ticketing/check-availability", json={
            "pnr": "SUPPNR1", "adult": 1, "child": 0, "price": 95000,
        })
    assert response.status_code == 200
    assert set(response.json()) <= {"request_id", "status", "available"}
    for leak in ("dhillu", "supplier", "vendor", "95000", "SUPPNR1"):
        assert leak not in response.text
    assert response.json()["status"] == ("complete" if outcome == "success" else "unavailable")


@pytest.mark.parametrize("path,body", [
    ("/inventory", None),
    ("/search", {"origin": "LHE", "destination": "RUH", "departure_date": "2026-12-01"}),
    ("/tickets/11111111-1111-4111-8111-111111111111", None),
    ("/check-availability", {"pnr": "X", "adult": 1, "child": 0, "price": 1}),
    ("/flight-quotes", {}),
])
def test_customer_http_responses_never_include_supplier_data(monkeypatch, path, body):
    from app.core.authentication import require_authenticated_user

    @contextmanager
    def transaction():
        yield object()

    monkeypatch.setattr("app.db.agencies.transaction", transaction)
    monkeypatch.setattr("app.db.users.ensure_user", lambda *_: SimpleNamespace(id="u", role="customer", role_id=1))
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_authenticated_user] = lambda: object()
    with patch("app.api.routes.group_ticketing.fetch_all_group_inventory", new=AsyncMock()) as upstream:
        client = TestClient(app)
        response = client.get("/v1/group-ticketing" + path) if body is None else client.post("/v1/group-ticketing" + path, json=body)
    assert response.status_code == 403
    assert response.json() == {"detail": "Search access denied."}
    upstream.assert_not_called()
