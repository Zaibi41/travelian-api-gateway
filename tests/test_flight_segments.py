"""Flight-by-flight detail of a connection on search results: each flight's airports and times, and the time on the
ground at each stop (travelian#37, #83, #84, #119, #122). SooperFare and Southair, through the search endpoint too."""

from app.services.live_flights import segment_views
from app.vendors.flights.sooperfare import normalize as sf_normalize
from app.vendors.flights.southair import normalize

from tests.sooperfare.test_agency_visibility import run_search
from tests.sooperfare.test_normalize import load
from tests.southair.fixtures import live_uat_search_data


def test_non_stop_is_one_segment_without_a_layover():
    [view] = segment_views([{"flight_number": "PK302", "origin": "khi", "destination": "LHE",
                             "departure_local": "2026-12-10T08:00", "arrival_local": "2026-12-10T09:45", "duration_minutes": 105}])
    assert (view.flight_number, view.origin, view.destination, view.duration_minutes, view.layover_minutes) == ("PK302", "KHI", "LHE", 105, None)


def test_two_stops_give_the_time_at_each_stop():
    views = segment_views([
        {"flight_number": "EY254", "origin": "LHE", "destination": "AUH", "departure_local": "2026-12-10T03:00", "arrival_local": "2026-12-10T05:30"},
        {"flight_number": "EY11", "origin": "AUH", "destination": "DOH", "departure_local": "2026-12-10T08:15", "arrival_local": "2026-12-10T08:30"},
        {"flight_number": "QR1", "origin": "DOH", "destination": "LHR", "departure_local": "2026-12-11T01:00", "arrival_local": "2026-12-11T06:00"},
    ])
    assert [(v.origin, v.layover_minutes) for v in views] == [("LHE", None), ("AUH", 165), ("DOH", 990)]  # overnight stop: 16h 30m


def test_unusable_detail_gives_no_segments_and_bad_times_no_layover():
    assert segment_views([]) == [] and segment_views(None) == []
    assert segment_views([{"origin": "LHE", "destination": None}]) == []  # an airport missing: no half itinerary
    views = segment_views([{"origin": "LHE", "destination": "AUH", "arrival_local": "2026-12-10T05:30"},
                           {"origin": "AUH", "destination": "LHR", "departure_local": "2026-12-10T04:00"},  # before arrival
                           {"origin": "LHR", "destination": "MAN", "departure_local": None}])
    assert [v.layover_minutes for v in views] == [None, None, None]
    assert segment_views([{"origin": "A", "destination": "B", "duration_minutes": True}])[0].duration_minutes is None


def test_sooperfare_one_stop_flight_keeps_each_flight_and_the_stop_time():
    flights = [o.flights[0] for r in sf_normalize.parse_search(load("search_PK"), "KHI", False) for o in r.options]
    one_stop = next(f for f in flights if f.stops == 1)
    views = segment_views(one_stop.segments)
    assert [(v.flight_number, v.origin, v.destination, v.duration_minutes, v.layover_minutes) for v in views] == [
        ("PK310", "KHI", "UET", 80, None), ("PK323", "UET", "LHE", 80, 60)]


def test_southair_connection_keeps_each_flight_and_the_stop_time():
    data = live_uat_search_data()
    flight = data["result"]["LHE-KHI"]["flights"]["6af1bc80e37bf"]
    first = flight["FLT_INFO"]["LHEKHI"]["INFO"]
    flight["FLT_INFO"] = {
        "LHEISB": {"INFO": {**first, "callsign": "Z8101", "arr_city": "ISB", "arr_date_time": "2026-11-19 01:30:00", "arr_dt_utc": "2026-11-18 20:30:00"}},
        "ISBKHI": {"INFO": {**first, "callsign": "Z8202", "dep_city": "ISB", "dep_date_time": "2026-11-19 02:30:00", "dep_dt_utc": "2026-11-18 21:30:00",
                            "arr_date_time": "2026-11-19 04:35:00", "arr_dt_utc": "2026-11-18 23:35:00"}},
    }
    (parsed,) = normalize.legs(data)[1][0].flights
    views = segment_views(parsed.segments)
    assert [(v.flight_number, v.origin, v.destination, v.duration_minutes, v.layover_minutes) for v in views] == [
        ("Z8101", "LHE", "ISB", 65, None), ("Z8202", "ISB", "KHI", 125, 60)]


def test_southair_non_stop_from_live_shape_is_one_segment():
    (parsed,) = normalize.legs(live_uat_search_data())[1][0].flights
    [view] = segment_views(parsed.segments)
    assert (view.origin, view.destination, view.departure_local, view.arrival_local, view.layover_minutes) == (
        "LHE", "KHI", "2026-11-19T00:25", "2026-11-19T02:35", None)


def test_search_endpoint_returns_segments_with_the_stop_time(monkeypatch):
    _, response = run_search(monkeypatch, "agency_owner", provider="PK")
    assert response.status_code == 200
    flights = response.json()["journeys"][0]["flights"]
    one_stop = next(f for f in flights if f.get("stops") == 1 and f["flight_number"].startswith("PK"))
    assert one_stop["stop_cities"] == ["UET"]
    assert [(s["origin"], s["destination"], s.get("layover_minutes")) for s in one_stop["segments"]] == [("KHI", "UET", None), ("UET", "LHE", 60)]
    direct = next(f for f in flights if f.get("stops") == 0 and f["flight_number"].startswith("PK"))
    assert len(direct["segments"]) == 1 and "layover_minutes" not in direct["segments"][0]
