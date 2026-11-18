"""SooperFare answers as read live from the gateway's static IP on 18 November 2026 (fixtures; no personal data)."""

import json
from pathlib import Path

from app.vendors.flights.sooperfare import normalize

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def test_one_way_airblue_options_fares_and_prices():
    [result] = normalize.parse_search(load("search_PA"), "KHI", two_way=False)
    assert result.provider == "PA" and result.search_ref_id
    assert [o.flights[0].flight_number for o in result.options] == ["PA400", "PA406"]
    flight = result.options[0].flights[0]
    assert (flight.origin, flight.destination, flight.departure_local, flight.arrival_local, flight.stops) == ("KHI", "LHE", "2026-12-10T08:00", "2026-12-10T10:00", 0)
    assert (flight.airline_code, flight.airline_name, flight.duration_minutes) == ("PA", "Airblue", 120)
    value = flight.fares[0]
    assert (value.name, value.signature, value.currency) == ("Value", "Value|EV|1", "PKR")
    assert value.per_type == {"ADT": (1, 2205000)} and value.adult_each_minor == 2205000
    assert value.baggage == "No checked baggage, 7 kg hand carry"
    assert value.change and value.change.startswith("<0 hours before departure: Rs 8,000")


def test_every_airline_parses_and_a_one_stop_flight_keeps_its_stop():
    counts = {name: sum(len(r.options) for r in normalize.parse_search(load(f"search_{name}"), "KHI", False)) for name in ("PA", "PF", "PK", "ONEAPI")}
    assert counts == {"PA": 2, "PF": 2, "PK": 3, "ONEAPI": 4}
    pia = [o.flights[0] for r in normalize.parse_search(load("search_PK"), "KHI", False) for o in r.options]
    one_stop = next(f for f in pia if f.stops == 1)
    assert one_stop.flight_number.count("/") == 1 and len(one_stop.stop_cities) == 1
    assert normalize.parse_search(load("search_Z8"), "KHI", False) == []  # no flights on this route
    assert normalize.parse_search(load("search_SABRE"), "KHI", False) == []  # refused provider


def test_return_options_pair_outbound_and_return_with_per_type_prices():
    [result] = normalize.parse_search(load("search_return_PA"), "KHI", two_way=True)
    assert len(result.options) == 4
    out, back = result.options[0].flights
    assert (out.origin, back.origin) == ("KHI", "LHE")
    assert out.fares[0].per_type == {"ADT": (2, 4278000), "CHD": (1, 1879800)}
    assert normalize.parse_search(load("search_return_PA"), "LHE", two_way=True) == []  # wrong direction is not offered


def test_quotes_sum_per_type_over_both_flights_and_list_passenger_slots():
    one = normalize.parse_quote(load("quote_PA"))
    assert one.per_type == {"ADT": (1, 2205000)} and one.currency == "PKR" and one.passport_required
    assert [p.kind for p in one.passengers] == ["ADT"]
    both = normalize.parse_quote(load("quote_return_PA"))
    assert both.per_type == {"ADT": (2, 4278000 + 4654000), "CHD": (1, 1879800 + 2020800)}
    assert [p.kind for p in both.passengers] == ["ADT", "ADT", "CHD"]
    assert "nic" in both.passengers[0].required_fields
    assert normalize.parse_quote({"status": True, "data": {"providers": []}}) is None
