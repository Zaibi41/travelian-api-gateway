"""An agency user's flight search and reprice through the real route and role resolution (travelian#130, D23).

Both suppliers answer (Southair and SooperFare, real fixtures, fake transport). The agency owner has every grant
row, which must be ignored: the JSON it receives carries no supplier name, no supplier cost, no margin, no provider
list and none of SooperFare's airline/provider identifiers, refs, signatures or CDN logo URLs. Super Admin, through
the same route, does receive the supplier fields (so the checks are not vacuous).
"""

import json
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
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

from tests.southair.fixtures import search_data

FIX = Path(__file__).parent / "fixtures"
BODY = {"origin": "KHI", "destination": "LHE", "departure_date": "2026-12-10", "adults": 1}
SUPPLIER_KEYS = {"supplier", "supplier_cost", "supplier_total", "margin", "providers"}


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def sooperfare_fake(provider):
    client = MagicMock()
    client.availability = AsyncMock(return_value=[provider])
    client.search = AsyncMock(return_value=load(f"search_{provider}"))
    client.quote = AsyncMock(return_value=load("quote_PA"))
    return client


def southair_fake():
    client = MagicMock()
    client.search = AsyncMock(return_value=search_data())
    client.profile = AsyncMock(return_value={"office": {"currency": "PKR"}})
    return client


@pytest.fixture(autouse=True)
def setup():
    live_flights._quotes.clear()
    live_flights._sessions.clear()
    live_flights._airlines.clear()
    live_flights._account_currency = None
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0):
        yield


def app_for(monkeypatch, role, granted, provider):
    @contextmanager
    def transaction():
        yield object()

    monkeypatch.setattr("app.db.agencies.transaction", transaction)
    monkeypatch.setattr("app.db.users.ensure_user", lambda *_: SimpleNamespace(id="u-" + role, role=role, role_id=9))
    monkeypatch.setattr("app.core.field_visibility.granted_permissions", lambda *_: set(granted))
    monkeypatch.setattr(live_flights.sooperfare, "configured", lambda: True)
    monkeypatch.setattr(live_flights.sooperfare, "SooperfareClient", lambda: sooperfare_fake(provider))
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_authenticated_user] = lambda: object()
    return TestClient(app)


def run_search(monkeypatch, role, granted=frozenset(), provider="ONEAPI"):
    client = app_for(monkeypatch, role, granted, provider)
    with patch.object(live_flights, "configured_client", southair_fake):
        return client, client.post("/v1/live-ticketing/flights/search", json=BODY)


def run_reprice(monkeypatch, role, granted=frozenset()):
    """Search (SooperFare's Airblue answer, which the quote fixture belongs to), then reprice its first fare."""
    client, search = run_search(monkeypatch, role, granted, provider="PA")
    body = search.json()
    flight = next(f for f in body["journeys"][0]["flights"] if f["flight_number"] == "PA400")
    with patch.object(live_flights, "configured_client", southair_fake):
        return client.post("/v1/live-ticketing/flights/reprice", json={"search_id": body["search_id"], "fare_ids": [flight["fares"][0]["id"]]})


def keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def supplier_identifiers(*fixtures):
    """Every value from SooperFare's answers that identifies the supplier or its records (refs, signatures, logo host)."""
    found = {"sooperfare", "southair", "cdn.sooperfare.com"}

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"ref_id", "signature"} and isinstance(item, str):
                    found.add(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for name in fixtures:
        walk(load(name))
    return found


def test_agency_search_gets_both_suppliers_results_without_any_supplier_field(monkeypatch):
    _, response = run_search(monkeypatch, "agency_owner", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN})  # grants ignored
    assert response.status_code == 200
    body = response.json()
    flights = body["journeys"][0]["flights"]
    numbers = {flight["flight_number"] for flight in flights}
    assert {"MM112", "MM114"} <= numbers and any(n.startswith("9P") for n in numbers)  # Southair and SooperFare both in
    assert not keys(body) & SUPPLIER_KEYS
    for flight in flights:
        assert set(flight) <= {"id", "airline_code", "airline_name", "flight_number", "departure_local", "arrival_local",
                               "duration_minutes", "stops", "stop_cities", "segments", "fares"}
        for row in flight.get("segments", []):
            assert set(row) <= {"flight_number", "airline_code", "airline_name", "operating_code", "operating_name", "origin",
                                 "destination", "departure_local", "arrival_local", "duration_minutes", "layover_minutes"}
        for fare in flight["fares"]:
            assert set(fare) <= {"id", "name", "meal", "baggage", "baggage_detail", "refund", "time_limit", "selling_price", "total_price", "passenger_prices", "booking_mode"}
    raw = response.text
    lowered = raw.lower()
    for secret in supplier_identifiers("search_ONEAPI") | {"ONEAPI"}:  # ONEAPI: SooperFare's airline provider id
        assert secret.lower() not in lowered, secret


def test_agency_reprice_gets_the_selling_total_without_supplier_total_or_margin(monkeypatch):
    response = run_reprice(monkeypatch, "agency_owner", {SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selling_total"]["amount_minor"] > 0
    assert not keys(body) & SUPPLIER_KEYS
    for secret in supplier_identifiers("search_PA", "quote_PA"):
        assert secret.lower() not in response.text.lower(), secret


def test_super_admin_through_the_same_routes_does_get_supplier_fields(monkeypatch):
    _, response = run_search(monkeypatch, "super_admin")
    body = response.json()
    assert {"supplier", "supplier_cost", "providers"} <= keys(body)
    assert {p["name"] for p in body["providers"]} == {"southair", "sooperfare"}
    priced = run_reprice(monkeypatch, "super_admin").json()
    assert "supplier_total" in priced and "margin" in priced
