"""Hotel search: normalizing, selling price, D23 visibility by role, D24 notice, routes and the supplier console."""

import asyncio
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.hotels import router as hotels_router
from app.api.routes.ratehawk_supplier import router as supplier_router
from app.core.authentication import require_authenticated_user
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, SEE_COST_AND_MARGIN, SEE_SUPPLIER_NAMES, BackOfficeUser
from app.schemas.hotels import HotelSearchRequest
from app.services import hotel_search
from app.vendors.hotels.ratehawk import normalize
from app.vendors.hotels.ratehawk.client import RateHawkError, RateHawkNotConfigured

from tests.ratehawk import fixtures

CHECK_IN = date.today() + timedelta(days=30)
BODY = {"destination": "Dubai", "check_in": CHECK_IN.isoformat(), "check_out": (CHECK_IN + timedelta(days=2)).isoformat(),
        "rooms": [{"adults": 2}]}


@pytest.fixture(autouse=True)
def reset():
    hotel_search._sessions.clear()
    hotel_search._content.clear()
    yield


def fake_client(content=None, serp=None):
    client = MagicMock()
    client.suggest = AsyncMock(return_value=fixtures.suggest())
    client.search_region = AsyncMock(return_value=serp if serp is not None else fixtures.serp())
    client.search_hotels = AsyncMock(return_value=fixtures.serp())
    client.content_by_ids = content or AsyncMock(return_value=fixtures.content())
    return client


def run_search(viewer, client, body=BODY):
    return asyncio.run(hotel_search.search(HotelSearchRequest(**body), viewer, client_factory=lambda: client))


# ---------- normalize ----------

def test_rate_cancellation_kinds_and_board():
    free = normalize.rate(fixtures.rate("96.00"))
    assert (free.cancellation, free.free_cancellation_until.isoformat()) == ("free_until", "2026-12-16T00:00:00")
    assert normalize.rate(fixtures.rate("80.00", free_until=None)).cancellation == "non_refundable"
    assert free.board == "Room only" and free.nights == 2
    assert normalize.board("half-board") == "Half board"
    assert normalize.board(None) == "Meal info not available"


def test_rate_without_price_is_dropped_and_zero_stars_is_none():
    assert normalize.rate({"payment_options": {"payment_types": []}}) is None
    assert normalize.content(fixtures.content()[1]).star_rating is None


# ---------- service ----------

def test_search_lists_hotels_cheapest_first_with_names_from_content():
    view = run_search(BackOfficeUser("u", "super_admin", ALL), fake_client())
    assert view.status == "complete" and view.destination == "Dubai"
    assert [h.name for h in view.hotels] == ["Hotel B", "Hotel A"]  # hid 103 has no content: left out
    a = view.hotels[1]
    assert [r.total.amount_minor for r in a.rates] == [9600, 12000]
    assert a.rates[0].per_night.amount_minor == 4800
    assert a.source == "RateHawk" and a.rates[0].supplier_cost.amount_minor == 9600
    assert view.providers[0].status == "responded"


def test_markup_adds_percent_rounded_up_to_a_cent():
    with patch.object(settings, "hotel_markup_percent", Decimal("5")):
        view = run_search(BackOfficeUser("u", "super_admin", ALL), fake_client())
    rate = view.hotels[1].rates[0]
    assert (rate.total.amount_minor, rate.supplier_cost.amount_minor) == (10080, 9600)


@pytest.mark.parametrize("visibility,names,cost", [
    (NONE, False, False),
    (ALL, True, True),
])
def test_visibility(visibility, names, cost):
    view = run_search(BackOfficeUser("u", "staff", visibility), fake_client())
    dumped = view.model_dump(exclude_none=True)
    assert ("providers" in dumped) is names
    assert all(("source" in h) is names for h in dumped["hotels"])
    assert all(("supplier_cost" in r) is cost for h in dumped["hotels"] for r in h["rates"])
    assert "m-96.00" not in str(dumped)  # supplier hashes never leave the gateway


def test_content_failure_returns_rest_with_notice():
    calls = AsyncMock(side_effect=[RateHawkError("timeout")])
    serp = {"hotels": [{"hid": 100 + i, "rates": [fixtures.rate("10.00")]} for i in range(150)]}
    hotel_search._content[100] = (hotel_search._now(), normalize.content({"hid": 100, "name": "Cached"}))
    view = run_search(BackOfficeUser("u", "staff", NONE), fake_client(content=calls, serp=serp))
    assert [h.name for h in view.hotels] == ["Cached"]
    assert view.notice == "Some results may be missing."


def test_supplier_failure_and_not_configured_are_unavailable_without_names_for_agency():
    failing = fake_client()
    failing.search_region = AsyncMock(side_effect=RateHawkError("timeout"))
    view = run_search(BackOfficeUser("u", "agency_owner", NONE), failing)
    assert view.status == "unavailable" and view.hotels == [] and view.providers is None and view.notice is None
    admin = asyncio.run(hotel_search.search(HotelSearchRequest(**BODY), BackOfficeUser("u", "super_admin", ALL),
                                            client_factory=lambda: (_ for _ in ()).throw(RateHawkNotConfigured())))
    assert admin.providers[0].status == "not_configured"


def test_chosen_hotel_searches_that_hotel_only():
    client = fake_client()
    run_search(BackOfficeUser("u", "staff", NONE), client, {**BODY, "destination_kind": "hotel", "destination_id": 101})
    client.search_hotels.assert_awaited_once()
    assert client.search_hotels.await_args.kwargs["hids"] == [101]
    client.suggest.assert_not_awaited()


def test_rate_ids_are_bound_to_the_user():
    view = run_search(BackOfficeUser("u1", "staff", NONE), fake_client())
    assert hotel_search.get_session(view.search_id, "u1") is not None
    assert hotel_search.get_session(view.search_id, "u2") is None


@pytest.mark.parametrize("change", [
    {"check_out": CHECK_IN.isoformat()},
    {"check_out": (CHECK_IN + timedelta(days=31)).isoformat()},
    {"check_in": (date.today() - timedelta(days=1)).isoformat()},
    {"rooms": []},
    {"rooms": [{"adults": 1, "children_ages": [18]}]},
    {"destination_id": 5},
])
def test_invalid_requests(change):
    with pytest.raises(ValueError):
        HotelSearchRequest(**{**BODY, **change})


# ---------- routes, through the real role resolution ----------

ROLES = {
    "super_admin": ("super_admin", set()),
    "staff": ("staff", set()),
    "staff_both": ("staff", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN}),
    "agency_owner": ("agency_owner", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN}),  # grants ignored
    "customer": ("customer", set()),
}


def app_for(monkeypatch, label):
    role, granted = ROLES[label]

    @contextmanager
    def transaction():
        yield object()

    monkeypatch.setattr("app.db.agencies.transaction", transaction)
    monkeypatch.setattr("app.db.users.ensure_user", lambda *_: SimpleNamespace(id="u-" + label, role=role, role_id=9))
    monkeypatch.setattr("app.core.field_visibility.granted_permissions", lambda *_: set(granted))
    app = FastAPI()
    app.include_router(hotels_router, prefix="/v1")
    app.include_router(supplier_router, prefix="/v1")
    app.dependency_overrides[require_authenticated_user] = lambda: object()
    return TestClient(app)


@pytest.mark.parametrize("label,status,sees", [
    ("super_admin", 200, True), ("staff", 200, False), ("staff_both", 200, True),
    ("agency_owner", 200, False), ("customer", 403, False),
])
def test_search_route_by_role(monkeypatch, label, status, sees):
    client = fake_client()
    with patch.object(hotel_search, "configured_client", lambda: client):
        response = app_for(monkeypatch, label).post("/v1/hotels/search", json=BODY)
    assert response.status_code == status
    if status == 200:
        body = response.json()
        assert ("providers" in body) is sees
        assert ("supplier_cost" in body["hotels"][0]["rates"][0]) is sees
        assert ("source" in body["hotels"][0]) is sees


def test_destinations_route(monkeypatch):
    client = fake_client()
    with patch.object(hotel_search, "configured_client", lambda: client):
        response = app_for(monkeypatch, "agency_owner").post("/v1/hotels/destinations", json={"query": "Dub"})
    assert response.json()["destinations"][0] == {"kind": "region", "id": 6053839, "name": "Dubai",
                                                   "region_type": "RT_CITY", "country_code": "AE"}


@pytest.mark.parametrize("label", ["staff", "staff_both", "agency_owner", "customer"])
def test_supplier_console_is_super_admin_only(monkeypatch, label):
    response = app_for(monkeypatch, label).get("/v1/admin/suppliers/ratehawk/operations")
    assert response.status_code == 403


def test_supplier_console_lists_and_calls_operations(monkeypatch):
    http = app_for(monkeypatch, "super_admin")
    assert len(http.get("/v1/admin/suppliers/ratehawk/operations").json()) == 48
    rh = MagicMock()
    rh.call = AsyncMock(return_value={"hid": 1})
    with patch("app.api.routes.ratehawk_supplier.configured_client", lambda: rh):
        response = http.post("/v1/admin/suppliers/ratehawk/operations/hotel_info", json={"payload": {"hid": 1, "language": "en"}})
    assert response.json() == {"operation": "hotel_info", "data": {"hid": 1}}
    rh.call.assert_awaited_once_with("hotel_info", {"hid": 1, "language": "en"})


def test_supplier_console_write_switch_and_unknown_outcome(monkeypatch):
    http = app_for(monkeypatch, "super_admin")
    with patch.object(settings, "ratehawk_endpoint", "https://rh.test"), patch.object(settings, "ratehawk_key_id", "1"), \
            patch.object(settings, "ratehawk_api_key", __import__("pydantic").SecretStr("k")):
        response = http.post("/v1/admin/suppliers/ratehawk/operations/order_cancel", json={"payload": {"partner_order_id": "x"}})
    assert (response.status_code, response.json()["code"]) == (409, "writes_disabled")
    rh = MagicMock()
    rh.call = AsyncMock(side_effect=RateHawkError("timeout"))
    with patch("app.api.routes.ratehawk_supplier.configured_client", lambda: rh):
        response = http.post("/v1/admin/suppliers/ratehawk/operations/booking_finish", json={"payload": {}})
    assert (response.status_code, response.json()["code"]) == (504, "outcome_unknown")
    response = http.post("/v1/admin/suppliers/ratehawk/operations/overview", json={})
    assert (response.status_code, response.json()["code"]) == (503, "not_configured")
