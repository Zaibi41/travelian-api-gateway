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

from tests.southair.fixtures import HASH_EB, HASH_LIVE, NLD, live_fare_info, live_fare_info_with, live_uat_search_data, search_data

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
    client = fake_client(search_data(), search_data(), fare_info={"FINAL_TOTAL": "915.40"})  # 2 x 457.70
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
    client = fake_client(search_data(), search_data(total="500.00"), fare_info={"FINAL_TOTAL": "1000.00"})
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
    assert refused.status_code == 422 and "Child and infant" in refused.json()["detail"] and refused.json()["code"] == "adults_only"


@pytest.mark.parametrize("case,status", [("gone", 409), ("expired", 404), ("wrong_count", 422), ("no_markup", 409), ("supplier_down", 502)])
def test_reprice_refusals(case, status):
    second = search_data(callsign="ZZ999") if case == "gone" else search_data()
    client = fake_client(search_data(), SouthairError("timeout") if case == "supplier_down" else second, fare_info={"FINAL_TOTAL": "915.40"})
    test, patcher, result = searched(STAFF, client)
    fare_ids = [guide_fare_id(result)] * (2 if case == "wrong_count" else 1)
    search_id = "x" * 20 if case == "expired" else result["search_id"]
    with patch.object(settings, "flight_markup_percent", Decimal("0" if case == "no_markup" else "2")):
        response = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": search_id, "fare_ids": fare_ids})
    patcher.stop()
    assert response.status_code == status, response.text
    expected_code = {"gone": "fare_gone", "expired": "search_expired", "wrong_count": "invalid_selection", "no_markup": "no_selling_price",
                     "supplier_down": "not_confirmed"}[case]
    assert response.json()["code"] == expected_code and response.json()["detail"]


def test_round_trip_reprices_both_legs():
    body = {"origin": "KHI", "destination": "MUX", "departure_date": "2026-11-27", "return_date": "2026-11-30", "adults": 1}
    client = fake_client(search_data(round_trip=True), search_data(round_trip=True), fare_info={"FINAL_TOTAL": "857.70"})
    test, patcher, result = searched(STAFF, client, body)
    out = guide_fare_id(result)
    ret = result["journeys"][1]["flights"][0]["fares"][0]["id"]
    assert test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [ret, out]}).status_code == 422
    priced = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [out, ret]}).json()
    patcher.stop()
    assert priced["selling_total"]["amount_minor"] == 87_500  # (457.70 + 400.00) x 1.02 = 874.85 -> 875


@pytest.mark.parametrize("enabled", [False, True])
def test_booking_enabled_follows_the_flag_set_here_not_the_env(enabled):
    client = fake_client(search_data(), search_data(), fare_info={"FINAL_TOTAL": "915.40"})
    test, patcher, result = searched(STAFF, client)
    with patch.object(settings, "southair_booking_enabled", enabled):
        body = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]}).json()
    patcher.stop()
    assert body["booking_enabled"] is enabled


LIVE_BODY = {"origin": "LHE", "destination": "KHI", "departure_date": "2026-11-19", "adults": 1}


def live_reprice(fare_info=None, fare_info_error=None, adults=1, fare="EB", children=0, infants=0):
    client = fake_client(live_uat_search_data(), live_uat_search_data(), fare_info=fare_info, fare_info_error=fare_info_error)
    test, patcher, result = searched(ADMIN, client, {**LIVE_BODY, "adults": adults, "children": children, "infants": infants})
    fare_id = next(f["id"] for f in result["journeys"][0]["flights"][0]["fares"] if f["name"] == fare)
    response = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [fare_id]})
    patcher.stop()
    return response, client


def test_live_shape_adults_only_is_verified_against_final_total():
    response, client = live_reprice(fare_info=live_fare_info(13330))
    assert response.status_code == 200, response.text
    body = response.json()
    # ACC 13,330 confirmed by FINAL_TOTAL 13,330; 13,330 x 1.02 = 13,596.60 -> Rs 13,597
    assert body["selling_total"] == {"amount_minor": 1_359_700, "currency": "PKR"}
    assert body["supplier_total"] == {"amount_minor": 1_333_000, "currency": "PKR"}
    assert client.fare_info.await_args.kwargs["selections"] == [("LHEKHI", HASH_LIVE["EB"])]
    assert HASH_LIVE["EB"] not in response.text and NLD not in response.text and "FINAL_TOTAL" not in response.text


def test_live_shape_two_adults_needs_final_total_for_both():
    ok, _ = live_reprice(fare_info=live_fare_info(26660), adults=2)
    assert ok.status_code == 200 and ok.json()["selling_total"]["amount_minor"] == 2 * 1_359_700
    # A FINAL_TOTAL that covers one adult only doesn't verify a two-adult total: refused.
    refused, _ = live_reprice(fare_info=live_fare_info(13330), adults=2)
    assert refused.status_code == 409 and "doesn't match" in refused.json()["detail"]


@pytest.mark.parametrize("case", ["missing", "unparseable", "empty", "fare_info_error"])
def test_live_shape_without_a_verified_total_is_refused(case):
    fare_info = {"missing": live_fare_info(None), "unparseable": live_fare_info("n/a"), "empty": {}}.get(case)
    response, client = live_reprice(fare_info=fare_info, fare_info_error=SouthairError("fare_info_failed") if case == "fare_info_error" else None)
    assert response.status_code == 502 and "didn't confirm the total" in response.json()["detail"]
    assert live_flights._quotes == {}  # nothing to hold on


def test_live_shape_total_mismatch_is_refused():
    response, _ = live_reprice(fare_info=live_fare_info(14000))
    assert response.status_code == 409 and live_flights._quotes == {}


def test_reprice_returns_old_and_new_totals_and_stores_the_change_on_the_quote():
    client = fake_client(search_data(), search_data(total="500.00"), fare_info={"FINAL_TOTAL": "1000.00"})
    test, patcher, result = searched(STAFF, client)
    body = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]}).json()
    patcher.stop()
    # 2 adults: search 457.70 -> 467 each (2%); now 500.00 -> 510 each
    assert body["price_changed"] is True
    assert body["old_total"] == {"amount_minor": 93_400, "currency": "PKR"}
    assert body["new_total"] == body["selling_total"] == {"amount_minor": 102_000, "currency": "PKR"}
    quote = live_flights.get_quote(body["quote_id"], "u2")
    assert quote.price_changed is True and quote.old_total_minor == 93_400 and quote.selling_total_minor == 102_000


def test_unchanged_price_is_not_flagged():
    client = fake_client(search_data(), search_data(), fare_info={"FINAL_TOTAL": "915.40"})
    test, patcher, result = searched(STAFF, client)
    body = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]}).json()
    patcher.stop()
    assert body["price_changed"] is False and body["old_total"] == body["new_total"]
    assert live_flights.get_quote(body["quote_id"], "u2").price_changed is False


@pytest.mark.parametrize("limit,status", [(None, 200), (10_000, 200), (8_600, 200), (8_599, 409)])
def test_optional_cap_on_price_increases(limit, status):
    client = fake_client(search_data(), search_data(total="500.00"), fare_info={"FINAL_TOTAL": "1000.00"})
    test, patcher, result = searched(STAFF, client)
    with patch.object(settings, "flight_price_change_limit_minor", limit):
        response = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]})
    patcher.stop()
    assert response.status_code == status  # increase: Rs 1,020 - Rs 934 = Rs 86 (8,600 paisa)
    if status == 409:
        assert "more than the allowed change" in response.json()["detail"] and live_flights._quotes == {}


@pytest.mark.parametrize("kind,final,each", [("INF", 15015, 171_900), ("CHD", 24028, 1_091_200)])
def test_live_shape_child_or_infant_priced_from_components_that_match_final_total(kind, final, each):
    response, _ = live_reprice(fare_info=live_fare_info_with(kind, final), **({"infants": 1} if kind == "INF" else {"children": 1}))
    assert response.status_code == 200, response.text
    body = response.json()
    # Infant 1,053 + 316 + 316 = 1,685 x 1.02 = 1,718.70 -> Rs 1,719; child 10,698 x 1.02 = 10,911.96 -> Rs 10,912
    prices = {p["type"]: p["selling_each"]["amount_minor"] for p in body["passengers"]}
    assert prices["ADT"] == 1_359_700 and prices[kind] == each
    assert body["supplier_total"]["amount_minor"] == final * 100


@pytest.mark.parametrize("fare_info", [
    live_fare_info_with("INF", 15016),        # components don't add up to FINAL_TOTAL
    live_fare_info_with("INF", None),         # no FINAL_TOTAL
    {**live_fare_info_with("INF", 15015), "INF": {"BASE": {"FARE": {"VALUE": "n/a"}}, "TAX": []}},
    {**live_fare_info_with("INF", 15015), "INF": {"BASE": {"FARE": {"VALUE": 1685}}, "TAX": [{"X": 1}]}},  # unseen TAX shape
])
def test_live_shape_infant_without_a_verified_breakdown_is_refused(fare_info):
    response, _ = live_reprice(fare_info=fare_info, infants=1)
    assert response.status_code == 422 and live_flights._quotes == {}


def test_reprice_sends_per_adult_old_new_and_difference_and_margin_only_with_cost_permission():
    for viewer, sees_cost in ((STAFF, False), (ADMIN, True)):
        client = fake_client(search_data(), search_data(total="500.00"), fare_info={"FINAL_TOTAL": "1000.00"})
        test, patcher, result = searched(viewer, client)
        body = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]}).json()
        patcher.stop()
        # 457.70 -> Rs 467 per adult (2%); now 500.00 -> Rs 510; difference Rs 43
        assert body["old_price_per_adult"]["amount_minor"] == 46_700 and body["new_price_per_adult"]["amount_minor"] == 51_000
        assert body["price_difference_per_adult"]["amount_minor"] == 4_300
        if sees_cost:
            assert body["margin"]["amount_minor"] == 102_000 - 100_000 and body["supplier_total"]["amount_minor"] == 100_000
        else:
            assert "margin" not in body and "supplier_total" not in body


def test_lower_price_gives_a_negative_difference():
    client = fake_client(search_data(), search_data(total="400.00"), fare_info={"FINAL_TOTAL": "800.00"})
    test, patcher, result = searched(STAFF, client)
    body = test.post("/v1/live-ticketing/flights/reprice", json={"search_id": result["search_id"], "fare_ids": [guide_fare_id(result)]}).json()
    patcher.stop()
    assert body["price_changed"] is True and body["price_difference_per_adult"]["amount_minor"] == 40_800 - 46_700
