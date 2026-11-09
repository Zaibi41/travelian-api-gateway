"""Southair search through /v1/live-ticketing/flights/search: normalization, D23, opaque IDs, D24."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.live_ticketing import router
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, require_search_viewer
from app.services import live_flights
from app.vendors.flights.southair import normalize
from app.vendors.flights.southair.client import SouthairError, SouthairNotConfigured

from tests.southair.fixtures import HASH_EB, HASH_RET, HASH_UAT, NLD, search_data

VIEWERS = {
    "super_admin": BackOfficeUser("u1", "super_admin", ALL),
    "staff": BackOfficeUser("u2", "staff", NONE),
    "staff_names": BackOfficeUser("u3", "staff", FieldVisibility(True, False)),
    "staff_cost": BackOfficeUser("u4", "staff", FieldVisibility(False, True)),
    "agency_owner": BackOfficeUser("u5", "agency_owner", NONE),
}
BODY = {"origin": "KHI", "destination": "MUX", "departure_date": "2026-11-27", "adults": 1}
SECRETS = (NLD, HASH_EB, HASH_UAT, HASH_RET, "southair", "Southair", "supplier", "45770", "30000", "MNM", "740")


def fake_client(data=None, error=None, profile=None):
    client = MagicMock()
    client.search = AsyncMock(return_value=data, side_effect=error)
    client.profile = AsyncMock(return_value=profile or {"office": {"currency": "PKR"}})
    return client


@pytest.fixture(autouse=True)
def reset():
    live_flights._sessions.clear()
    live_flights._account_currency = None
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0):
        yield


def call(viewer, body=BODY, client=None):
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: viewer
    client = client or fake_client(search_data())
    with patch("app.vendors.flights.southair.client.configured_client", lambda: client), \
         patch.object(live_flights, "configured_client", lambda: client):
        return TestClient(app).post("/v1/live-ticketing/flights/search", json=body)


def test_normalizes_both_fare_shapes():
    nld, legs = normalize.legs(search_data())
    assert nld == NLD and len(legs) == 1 and legs[0].route_key == "KHIMUX"
    guide, uat = legs[0].flights
    assert guide.airline_code == "MM" and guide.flight_number == "MM112" and guide.duration_minutes == 60 and guide.stops == 0
    assert guide.departure_local == "2026-11-27T23:00" and guide.stop_cities == []
    assert [f.code for f in guide.fares] == ["EB"]  # "NA" acceptance dropped
    assert guide.fares[0].adult_total == normalize.Amount(Decimal("457.70"), "PKR") and guide.fares[0].refund == "Refundable"
    assert [f.code for f in uat.fares] == ["Y"]  # STATUS 0 dropped
    assert uat.fares[0].adult_total == normalize.Amount(Decimal("300.00"), None)


@pytest.mark.parametrize("role", ["staff", "agency_owner"])
def test_staff_and_agency_get_opaque_ids_and_selling_price_only(role):
    response = call(VIEWERS[role])
    assert response.status_code == 200
    for secret in SECRETS:
        assert secret not in response.text, secret
    body = response.json()
    flights = body["journeys"][0]["flights"]
    assert [f["flight_number"] for f in flights] == ["MM114", "MM112"]  # earliest first
    fare = flights[1]["fares"][0]
    assert fare["selling_price"] == {"amount_minor": 46_700, "currency": "PKR"}  # 457.70 + 2% = 466.85 -> 467
    assert set(fare) <= {"id", "name", "meal", "baggage", "refund", "time_limit", "selling_price"}
    # The UAT fare had no currency: the account's profile currency is used.
    assert flights[0]["fares"][0]["selling_price"] == {"amount_minor": 30_600, "currency": "PKR"}
    assert "providers" not in body


def test_permissions_add_supplier_and_cost_independently():
    admin = call(VIEWERS["super_admin"]).json()
    fare = admin["journeys"][0]["flights"][1]["fares"][0]
    assert fare["supplier"] == "southair" and fare["supplier_cost"] == {"amount_minor": 45_770, "currency": "PKR"}
    assert admin["providers"] == [{"name": "southair", "status": "responded"}]
    names = call(VIEWERS["staff_names"]).json()["journeys"][0]["flights"][1]["fares"][0]
    assert "supplier" in names and "supplier_cost" not in names
    cost = call(VIEWERS["staff_cost"]).json()["journeys"][0]["flights"][1]["fares"][0]
    assert "supplier_cost" in cost and "supplier" not in cost
    for viewer in VIEWERS.values():
        raw = call(viewer).text
        assert NLD not in raw and HASH_EB not in raw and HASH_UAT not in raw


def test_fare_ids_are_opaque_server_side_and_bound_to_the_user():
    body = call(VIEWERS["staff"]).json()
    fare_id = body["journeys"][0]["flights"][1]["fares"][0]["id"]
    session = live_flights.get_session(body["search_id"], "u2")
    assert session.nld == NLD and session.fares[fare_id].fare_hash == HASH_EB and session.fares[fare_id].route_key == "KHIMUX"
    assert live_flights.get_session(body["search_id"], "someone-else") is None
    with patch.object(live_flights, "_now", lambda: session.expires_at + 1):
        assert live_flights.get_session(body["search_id"], "u2") is None


def test_round_trip_has_two_journeys():
    body = call(VIEWERS["staff"], {**BODY, "return_date": "2026-11-30"}, fake_client(search_data(round_trip=True))).json()
    assert [(j["direction"], j["origin"], j["destination"]) for j in body["journeys"]] == [("outbound", "KHI", "MUX"), ("return", "MUX", "KHI")]


@pytest.mark.parametrize("error", [SouthairError("timeout", "x"), SouthairNotConfigured()])
def test_provider_failure_is_unavailable_without_names_or_missing_notice(error):
    staff = call(VIEWERS["staff"], client=fake_client(error=error)).json()
    assert staff["status"] == "unavailable" and staff["journeys"] == []
    assert "notice" not in staff  # nothing came back, so not "some results may be missing"
    assert "southair" not in json.dumps(staff).lower() and "not_configured" not in json.dumps(staff)
    admin = call(VIEWERS["super_admin"], client=fake_client(error=error)).json()
    assert admin["providers"][0]["name"] == "southair"


def test_not_configured_client_reports_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "southair_base_url", None)
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: VIEWERS["staff"]
    assert TestClient(app).post("/v1/live-ticketing/flights/search", json=BODY).json()["status"] == "unavailable"


def test_no_markup_means_no_price():
    with patch.object(settings, "flight_markup_percent", Decimal("0")):
        fare = call(VIEWERS["staff"]).json()["journeys"][0]["flights"][1]["fares"][0]
    assert "selling_price" not in fare


def test_signed_out_is_401(monkeypatch):
    monkeypatch.setattr(settings, "cognito_issuer", "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Test1")
    monkeypatch.setattr(settings, "cognito_app_client_id", "testclient123")
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    assert TestClient(app).post("/v1/live-ticketing/flights/search", json=BODY).status_code == 401
