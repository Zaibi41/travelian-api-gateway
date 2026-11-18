"""The live UAT response shape (no FLT_MAIN, ACC, HIGHLIGHT) through normalize and the search endpoint."""

import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.vendors.flights.southair import normalize

from tests.southair.fixtures import HASH_LIVE, NLD, live_uat_search_data
from tests.southair.test_search import VIEWERS, call, fake_client

RAW_FIELDS = ("ACC", "HIGHLIGHT", "feature_code", "icon_code", "FLT_INFO", "AVB", "CABIN", "STOP_COUNT", "flight_time",
              "dep_date_time", "Nill Baggage", "Validity", "fa fa-", "airline_id", "ticket_code", "SOA", "995", "ATR72")


def test_normalizes_times_stops_duration_price_and_baggage():
    nld, legs = normalize.legs(live_uat_search_data())
    assert nld == NLD and [(leg.origin, leg.destination, leg.route_key) for leg in legs] == [("LHE", "KHI", "LHEKHI")]
    (flight,) = legs[0].flights
    assert (flight.flight_number, flight.airline_code, flight.airline_name) == ("Z8266", "Z8", "South Air")
    assert (flight.departure_local, flight.arrival_local) == ("2026-11-19T00:25", "2026-11-19T02:35")
    assert (flight.duration_minutes, flight.stops, flight.stop_cities) == (130, 0, [])
    fares = {fare.code: fare for fare in flight.fares}
    assert {code: fare.adult_total.value for code, fare in fares.items()} == {"EB": 13330, "EV": 14924, "EP": 16518}
    assert {code: fare.baggage for code, fare in fares.items()} == {"EB": "No checked baggage", "EV": "20KG", "EP": "30KG"}
    assert all(fare.meal is None and fare.refund is None and fare.time_limit is None for fare in fares.values())
    assert {code: fare.fare_hash for code, fare in fares.items()} == HASH_LIVE  # kept server-side


@pytest.mark.parametrize("raw,shown", [
    ("Nill Baggage", "No checked baggage"), ("Nil Baggage", "No checked baggage"), ("NIL", "No checked baggage"),
    ("No Baggage", "No checked baggage"), ("0KG", "No checked baggage"), ("0 KG", "No checked baggage"),
    ("None", "No checked baggage"), ("No checked baggage", "No checked baggage"),
    ("20KG", "20KG"), ("30KG", "30KG"), ("1 PC 23KG", "1 PC 23KG"), ("Nil cabin bag only", "Nil cabin bag only"), (None, None),
])
def test_baggage_mapping(raw, shown):
    assert normalize.baggage_text(raw) == shown


@pytest.mark.parametrize("value,minutes", [(130, 130), ("130", 130), ("01h 00m", 60), ("2h", 120), ("", None), ("abc", None)])
def test_duration_accepts_plain_minutes(value, minutes):
    assert normalize._minutes(value) == minutes


def test_connecting_flight_without_flt_main_uses_the_ends_and_counts_stops():
    data = live_uat_search_data()
    flight = data["result"]["LHE-KHI"]["flights"]["6af1bc80e37bf"]
    first = flight["FLT_INFO"]["LHEKHI"]["INFO"]
    flight["FLT_INFO"] = {
        "LHEISB": {"INFO": {**first, "arr_city": "ISB", "arr_date_time": "2026-11-19 01:30:00", "arr_dt_utc": "2026-11-18 20:30:00"}},
        "ISBKHI": {"INFO": {**first, "dep_city": "ISB", "dep_date_time": "2026-11-19 02:30:00", "dep_dt_utc": "2026-11-18 21:30:00",
                            "arr_date_time": "2026-11-19 04:35:00", "arr_dt_utc": "2026-11-18 23:35:00"}},
    }
    (parsed,) = normalize.legs(data)[1][0].flights
    assert (parsed.departure_local, parsed.arrival_local) == ("2026-11-19T00:25", "2026-11-19T04:35")
    assert (parsed.stops, parsed.stop_cities, parsed.duration_minutes) == (1, ["ISB"], 250)


@pytest.mark.parametrize("role", list(VIEWERS))
def test_every_role_gets_times_and_baggage_but_no_raw_supplier_field_or_token(role):
    response = call(VIEWERS[role], {"origin": "LHE", "destination": "KHI", "departure_date": "2026-11-19", "adults": 1},
                    fake_client(live_uat_search_data()))
    assert response.status_code == 200
    (flight,) = response.json()["journeys"][0]["flights"]
    assert (flight["departure_local"], flight["arrival_local"], flight["duration_minutes"], flight["stops"]) == ("2026-11-19T00:25", "2026-11-19T02:35", 130, 0)
    assert [(fare["name"], fare["baggage"]) for fare in flight["fares"]] == [("EB", "No checked baggage"), ("EV", "20KG"), ("EP", "30KG")]
    assert all("meal" not in fare and "refund" not in fare and "time_limit" not in fare for fare in flight["fares"])
    body = response.json()
    # Opaque IDs are random: check every value except the IDs for raw supplier fields.
    text = json.dumps({**body, "search_id": None, "journeys": [{**j, "flights": [{**f, "id": None, "fares": [{**x, "id": None} for x in f["fares"]]} for f in j["flights"]]} for j in body["journeys"]]})
    assert all(secret not in response.text for secret in (NLD, *HASH_LIVE.values()))
    for secret in (NLD, *HASH_LIVE.values(), "6af1bc80e37bf"):
        assert secret not in text
    for raw in RAW_FIELDS:
        assert raw not in text, raw
    if not VIEWERS[role].visibility.supplier_names:
        assert "southair" not in text.lower()


def test_price_is_supplier_amount_plus_markup_and_cost_only_with_permission():
    body = {"origin": "LHE", "destination": "KHI", "departure_date": "2026-11-19", "adults": 1}
    with patch.object(settings, "flight_markup_percent", Decimal("0")):
        fare = call(VIEWERS["super_admin"], body, fake_client(live_uat_search_data())).json()["journeys"][0]["flights"][0]["fares"][0]
    assert fare["selling_price"]["amount_minor"] == 1_333_000 and fare["supplier_cost"]["amount_minor"] == 1_333_000  # 0% markup
    with patch.object(settings, "flight_markup_percent", Decimal("5")):
        admin = call(VIEWERS["super_admin"], body, fake_client(live_uat_search_data())).json()["journeys"][0]["flights"][0]["fares"]
        staff = call(VIEWERS["staff"], body, fake_client(live_uat_search_data())).json()["journeys"][0]["flights"][0]["fares"]
    # 13,330 x 1.05 = 13,996.50, rounded up to Rs 13,997
    assert [f["selling_price"]["amount_minor"] for f in admin] == [1_399_700, 1_567_100, 1_734_400]
    assert [f["supplier_cost"]["amount_minor"] for f in admin] == [1_333_000, 1_492_400, 1_651_800]
    assert [f["selling_price"]["amount_minor"] for f in staff] == [1_399_700, 1_567_100, 1_734_400]
    assert all("supplier_cost" not in f and "supplier" not in f for f in staff)


def test_pnr_state_from_the_recorded_uat_shape():
    from tests.southair.fixtures import pnr_info
    assert normalize.pnr_state(pnr_info(), "63B55C").kind == "not_issued"
    assert normalize.pnr_state(pnr_info(tickets=("2147483647001", "2147483647002"), paid=457.70), "63B55C").kind == "issued"
    for unclear in (pnr_info(tickets=("2147483647001", None)), pnr_info(paid=457.70), pnr_info(status="XX"),
                    pnr_info(pnr="OTHER1"), {"names": []}, None):
        assert normalize.pnr_state(unclear, "63B55C").kind == "unclear"
    assert normalize.pnr_state(pnr_info(), "63B55C").tickets == [("ADT", None), ("INF", None)]


def test_cash_amount_is_digits_only():
    assert normalize.cash_amount(normalize.Amount(Decimal("13330"), None)) == "13330"
    assert normalize.cash_amount(normalize.Amount(Decimal("457.70"), None)) == "457.7"
    assert normalize.cash_amount(normalize.Amount(Decimal("13330.00"), None)) == "13330"
