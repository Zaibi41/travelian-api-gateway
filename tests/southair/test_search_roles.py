"""POST /v1/live-ticketing/flights/search by role, through the real role resolution (D23, D24).

`providers[].name` only for super_admin and staff with "see supplier names"; every
other role gets no `providers` and no supplier name or "not_configured" detail.
"""

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.live_ticketing import router
from app.core.authentication import require_authenticated_user
from app.core.config import settings
from app.core.field_visibility import SEE_COST_AND_MARGIN, SEE_SUPPLIER_NAMES
from app.services import live_flights
from app.vendors.flights.southair.client import SouthairError, SouthairNotConfigured

from tests.southair.fixtures import search_data

BODY = {"origin": "KHI", "destination": "MUX", "departure_date": "2026-11-27", "adults": 1}
ROLES = {  # label -> (gateway role, granted permissions)
    "super_admin": ("super_admin", set()),
    "staff": ("staff", set()),
    "staff_names": ("staff", {SEE_SUPPLIER_NAMES}),
    "staff_cost": ("staff", {SEE_COST_AND_MARGIN}),
    "agency_owner": ("agency_owner", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN}),  # grants ignored
    "customer": ("customer", {SEE_SUPPLIER_NAMES}),
    "admin": ("admin", {SEE_SUPPLIER_NAMES}),  # not a gateway role yet
    "sub_agency": ("sub_agency", {SEE_SUPPLIER_NAMES}),  # not a gateway role yet
}
SEES_PROVIDERS = {"super_admin", "staff_names"}
OUTCOMES = {
    "responded": lambda client: setattr(client.search, "return_value", search_data()),
    "failed": lambda client: setattr(client.search, "side_effect", SouthairError("timeout", "x")),
    "not_configured": lambda client: setattr(client.search, "side_effect", SouthairNotConfigured()),
}


@pytest.fixture(autouse=True)
def reset():
    live_flights._sessions.clear()
    live_flights._account_currency = None
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0):
        yield


def search(monkeypatch, label, outcome="responded"):
    role, granted = ROLES[label]

    @contextmanager
    def transaction():
        yield object()

    monkeypatch.setattr("app.db.agencies.transaction", transaction)
    monkeypatch.setattr("app.db.users.ensure_user", lambda *_: SimpleNamespace(id="u-" + label, role=role, role_id=9))
    monkeypatch.setattr("app.core.field_visibility.granted_permissions", lambda *_: set(granted))
    client = MagicMock()
    client.search = AsyncMock()
    client.profile = AsyncMock(return_value={"office": {"currency": "PKR"}})
    OUTCOMES[outcome](client)
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_authenticated_user] = lambda: object()
    with patch.object(live_flights, "configured_client", lambda: client):
        return TestClient(app).post("/v1/live-ticketing/flights/search", json=BODY)


@pytest.mark.parametrize("outcome", list(OUTCOMES))
@pytest.mark.parametrize("label", ["super_admin", "staff", "staff_names", "staff_cost", "agency_owner"])
def test_provider_names_only_with_the_supplier_names_permission(monkeypatch, label, outcome):
    response = search(monkeypatch, label, outcome)
    assert response.status_code == 200
    body = response.json()
    if label in SEES_PROVIDERS:
        assert body["providers"] == [{"name": "southair", "status": outcome}]
    else:
        assert "providers" not in body
        assert "southair" not in response.text.lower() and "not_configured" not in response.text


@pytest.mark.parametrize("outcome", list(OUTCOMES))
@pytest.mark.parametrize("label", ["customer", "admin", "sub_agency"])
def test_customer_and_unknown_roles_are_refused(monkeypatch, label, outcome):
    response = search(monkeypatch, label, outcome)
    assert response.status_code == 403 and response.json() == {"detail": "Search access denied."}


@pytest.mark.parametrize("label", ["super_admin", "staff", "agency_owner"])
def test_no_missing_results_notice_when_nothing_came_back(monkeypatch, label):
    for outcome in ("failed", "not_configured"):
        body = search(monkeypatch, label, outcome).json()
        assert body["status"] == "unavailable" and body["journeys"] == [] and "notice" not in body
    assert "notice" not in search(monkeypatch, label, "responded").json()
