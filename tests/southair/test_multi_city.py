"""Multi-city: every flight searched as its own one-way, one journey per flight, one fare per leg, booked by request."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.routes.live_ticketing import router
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, require_search_viewer
from app.schemas.flights import FlightSearchRequest
from app.services import live_flights

from tests.southair.fixtures import NLD, search_data

STAFF = BackOfficeUser("u2", "staff", NONE)
ADMIN = BackOfficeUser("u1", "super_admin", ALL)

TRIP = {"origin": "KHI", "destination": "MUX", "departure_date": "2026-11-27", "adults": 1,
        "legs": [{"origin": "KHI", "destination": "MUX", "departure_date": "2026-11-27"},
                 {"origin": "MUX", "destination": "KHI", "departure_date": "2026-11-30"}]}


def fake_client(fare_info=None):
    client = MagicMock()
    # Every one-way search answers with both routes; each leg keeps only the flights leaving its own origin.
    client.search = AsyncMock(side_effect=lambda **_: search_data(round_trip=True))
    client.fare_info = AsyncMock(return_value=fare_info or {})
    client.profile = AsyncMock(return_value={"office": {"currency": "PKR"}})
    return client


@pytest.fixture(autouse=True)
def reset():
    live_flights._sessions.clear()
    live_flights._quotes.clear()
    live_flights._account_currency = None
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0), \
            patch.object(live_flights.sooperfare, "configured", lambda: False), patch.object(live_flights.kyte, "configured", lambda: False):
        yield


def api(viewer, client):
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: viewer
    patcher = patch.object(live_flights, "configured_client", lambda: client)
    patcher.start()
    return TestClient(app), patcher


def test_each_leg_is_searched_as_a_one_way_and_answered_as_its_own_journey():
    client = fake_client()
    test, patcher = api(STAFF, client)
    response = test.post("/v1/live-ticketing/flights/search", json=TRIP)
    patcher.stop()
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(j["leg"], j["direction"], j["origin"], j["destination"]) for j in body["journeys"]] == [
        (1, "outbound", "KHI", "MUX"), (2, "outbound", "MUX", "KHI")]
    assert [f["flight_number"] for f in body["journeys"][1]["flights"]] == ["MM115"]
    assert {call.kwargs["return_date"] for call in client.search.await_args_list} == {None}
    assert sorted(str(call.kwargs["departure"]) for call in client.search.await_args_list) == ["2026-11-27", "2026-11-30"]
    assert all(fare["booking_mode"] == "request" for j in body["journeys"] for f in j["flights"] for fare in f["fares"])
    assert NLD not in response.text


def test_reprice_needs_one_fare_per_leg_in_order_and_sums_the_legs():
    client = fake_client(fare_info={"FINAL_TOTAL": "457.70"})
    test, patcher = api(ADMIN, client)
    result = test.post("/v1/live-ticketing/flights/search", json=TRIP).json()
    first = next(f for f in result["journeys"][0]["flights"] if f["flight_number"] == "MM112")["fares"][0]["id"]
    second = result["journeys"][1]["flights"][0]["fares"][0]["id"]
    wrong = [test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": ids})
             for ids in ([first], [second, first])]
    client.fare_info = AsyncMock(side_effect=[{"FINAL_TOTAL": "457.70"}, {"FINAL_TOTAL": "400.00"}])
    response = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [first, second]})
    patcher.stop()
    assert [r.status_code for r in wrong] == [422, 422] and {r.json()["code"] for r in wrong} == {"invalid_selection"}
    assert response.status_code == 200, response.text
    body = response.json()
    # Each flight sold on its own: 457.70 + 2% -> 467, 400.00 + 2% -> 408.
    assert body["selling_total"] == {"amount_minor": 46_700 + 40_800, "currency": "PKR"}
    assert body["booking_mode"] == "request" and body["booking_enabled"] is False and body["price_changed"] is False
    assert body["supplier_total"] == {"amount_minor": 85_770, "currency": "PKR"}
    quote = live_flights.get_quote(body["quote_id"], "u1")
    assert [ref.leg for ref in quote.refs] == [1, 2] and [ref.segment["leg"] for ref in quote.refs] == [1, 2]
    assert [ref.segment["origin"] for ref in quote.refs] == ["KHI", "MUX"]


def test_hold_is_refused_for_a_multi_city_quote():
    import asyncio

    from app.schemas.live_flights import HoldRequest
    from app.services import flight_holds

    client = fake_client()
    test, patcher = api(STAFF, client)
    result = test.post("/v1/live-ticketing/flights/search", json=TRIP).json()
    first = next(f for f in result["journeys"][0]["flights"] if f["flight_number"] == "MM112")["fares"][0]["id"]
    second = result["journeys"][1]["flights"][0]["fares"][0]["id"]
    client.fare_info = AsyncMock(side_effect=[{"FINAL_TOTAL": "457.70"}, {"FINAL_TOTAL": "400.00"}])
    quote = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [first, second]}).json()
    patcher.stop()
    request = HoldRequest.model_validate({
        "quote_id": quote["quote_id"], "confirmed_total": quote["selling_total"],
        "contact": {"first_name": "Ali", "last_name": "Khan", "phone_code": "92", "phone": "3001234567", "email": "a@example.com"},
        "passengers": [{"type": "ADT", "title": "MR", "first_name": "Ali", "last_name": "Khan", "date_of_birth": "1990-01-01",
                        "id_type": "NATI", "document_number": "3520112345671", "nationality": "PAK"}]})
    with patch.object(settings, "southair_booking_enabled", True):
        with pytest.raises(flight_holds.HoldError) as refused:
            asyncio.run(flight_holds.hold(request, STAFF))
    assert refused.value.code == "multi_city_request"


@pytest.mark.parametrize("change,message", [
    ({"return_date": "2026-12-01"}, "no return_date"),
    ({"origin": "LHE"}, "first leg"),
    ({"legs": [TRIP["legs"][0], {"origin": "MUX", "destination": "KHI", "departure_date": "2026-11-26"}]}, "on or after"),
    ({"legs": [TRIP["legs"][0]]}, "at least 2"),
    ({"legs": [TRIP["legs"][0], {"origin": "MUX", "destination": "MUX", "departure_date": "2026-11-30"}]}, "must be different"),
])
def test_multi_city_request_checks(change, message):
    with pytest.raises(ValidationError, match=message):
        FlightSearchRequest.model_validate({**TRIP, **change})


def test_last_travel_date_is_the_last_leg():
    assert str(FlightSearchRequest.model_validate(TRIP).last_travel_date) == "2026-11-30"
