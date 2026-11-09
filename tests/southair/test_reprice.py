"""Reprice: the gateway repeats the search for the selected fare and returns the total to confirm."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.live_ticketing import router
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, require_search_viewer
from app.services import live_flights
from app.vendors.flights.southair.client import SouthairError

from tests.southair.fixtures import HASH_EB, NLD, search_data

STAFF = BackOfficeUser("u2", "staff", NONE)
ADMIN = BackOfficeUser("u1", "super_admin", ALL)


def fake_client(*searches, fare_info=None, fare_info_error=None):
    client = MagicMock()
    client.search = AsyncMock(side_effect=list(searches))
    client.fare_info = AsyncMock(return_value=fare_info or {}, side_effect=fare_info_error)
    client.profile = AsyncMock(return_value={"office": {"currency": "PKR"}})
    return client


@pytest.fixture(autouse=True)
def reset():
    live_flights._sessions.clear()
    live_flights._quotes.clear()
    live_flights._account_currency = None
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0):
        yield


def api(viewer, client):
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: viewer
    patcher = patch.object(live_flights, "configured_client", lambda: client)
    patcher.start()
    return TestClient(app), patcher


def searched(viewer, client, body=None):
    test, patcher = api(viewer, client)
    result = test.post("/v1/live-ticketing/flights/search", json=body or {"origin": "KHI", "destination": "MUX", "departure_date": "2026-11-27", "adults": 2}).json()
    return test, patcher, result


def guide_fare_id(result):
    return next(f for f in result["journeys"][0]["flights"] if f["flight_number"] == "MM112")["fares"][0]["id"]


def test_reprice_repeats_the_search_and_returns_a_total_to_confirm():
    client = fake_client(search_data(), search_data(), fare_info_error=SouthairError("fare_info_failed"))
    test, patcher, result = searched(STAFF, client)
    response = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]})
    patcher.stop()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selling_total"] == {"amount_minor": 93_400, "currency": "PKR"}  # 2 x (457.70 + 2% -> 467)
    assert body["passengers"] == [{"type": "ADT", "count": 2, "selling_each": {"amount_minor": 46_700, "currency": "PKR"}}]
    assert body["price_changed"] is False and body["booking_enabled"] is False
    assert "supplier_total" not in body and NLD not in response.text and HASH_EB not in response.text
    assert client.search.await_count == 2
    quote = live_flights.get_quote(body["quote_id"], "u2")
    assert quote.supplier_total_minor == 91_540 and quote.nld == NLD and quote.refs[0].fare_hash == HASH_EB
    assert live_flights.get_quote(body["quote_id"], "someone-else") is None


def test_price_change_is_flagged_and_cost_only_for_permitted_viewers():
    client = fake_client(search_data(), search_data(total="500.00"))
    test, patcher, result = searched(ADMIN, client)
    body = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]}).json()
    patcher.stop()
    assert body["price_changed"] is True and body["selling_total"]["amount_minor"] == 2 * 51_000
    assert body["supplier_total"] == {"amount_minor": 100_000, "currency": "PKR"}


def test_children_need_explicit_fare_info_totals():
    body = {"origin": "KHI", "destination": "MUX", "departure_date": "2026-11-27", "adults": 1, "children": 1}
    client = fake_client(search_data(), search_data(), fare_info={"ADT": {"TOTAL": "457.70"}, "CHD": {"TOTAL": "300.00"}})
    test, patcher, result = searched(STAFF, client, body)
    ok = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]}).json()
    assert [(p["type"], p["selling_each"]["amount_minor"]) for p in ok["passengers"]] == [("ADT", 46_700), ("CHD", 30_600)]
    assert client.fare_info.await_args.kwargs["selections"] == [("KHIMUX", HASH_EB)]
    patcher.stop()
    client = fake_client(search_data(), search_data(), fare_info={"ADT": {"BASE": {"FARE": {"VALUE": 66000}}}})
    test, patcher, result = searched(STAFF, client, body)
    refused = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]})
    patcher.stop()
    assert refused.status_code == 422 and "Child and infant" in refused.json()["detail"]


@pytest.mark.parametrize("case,status", [("gone", 409), ("expired", 404), ("wrong_count", 422), ("no_markup", 409), ("supplier_down", 502)])
def test_reprice_refusals(case, status):
    second = search_data(callsign="ZZ999") if case == "gone" else search_data()
    client = fake_client(search_data(), SouthairError("timeout") if case == "supplier_down" else second)
    test, patcher, result = searched(STAFF, client)
    fare_ids = [guide_fare_id(result)] * (2 if case == "wrong_count" else 1)
    search_id = "x" * 20 if case == "expired" else result["search_id"]
    with patch.object(settings, "flight_markup_percent", Decimal("0" if case == "no_markup" else "2")):
        response = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": search_id, "fare_ids": fare_ids})
    patcher.stop()
    assert response.status_code == status, response.text


def test_round_trip_reprices_both_legs():
    body = {"origin": "KHI", "destination": "MUX", "departure_date": "2026-11-27", "return_date": "2026-11-30", "adults": 1}
    client = fake_client(search_data(round_trip=True), search_data(round_trip=True), fare_info_error=SouthairError("x"))
    test, patcher, result = searched(STAFF, client, body)
    out = guide_fare_id(result)
    ret = result["journeys"][1]["flights"][0]["fares"][0]["id"]
    assert test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [ret, out]}).status_code == 422
    priced = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [out, ret]}).json()
    patcher.stop()
    assert priced["selling_total"]["amount_minor"] == 87_500  # (457.70 + 400.00) x 1.02 = 874.85 -> 875
