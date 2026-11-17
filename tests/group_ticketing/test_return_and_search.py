"""Return group tickets (both flights listed, held and shown) and the search with any one of From, To or date.
Mocked data only. Skypass rows follow the live "Two Way" shape (sector_type Out / In, read 15 Nov 2026). Dhillu & Sahi
and Al Haider listed no return ticket live: their return rows here are inferred (more flights, turning back at the
destination)."""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from fastapi.testclient import TestClient

from app.api.routes import group_ticketing as group_routes
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, require_search_viewer, resolve_actions
from app.db.group_holds import GroupBooking
from app.services import group_holds
from app.services.group_ticketing import filter_group_inventory
from app.services.group_ticketing_view import baggage_text, ticket_view
from app.vendors.group_ticketing.al_haider.inventory import normalize as al_haider_normalize
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups
from app.vendors.group_ticketing.skypass.inventory import SkypassTicket, _normalize_ticket
from tests.group_ticketing.test_al_haider_and_sectors import al_group

SKY_SECTOR = {"flight_code": "9P", "baggage": "20+10", "meal": "No"}
SKY_ONE_WAY = {"id": 12001, "name": "Fly Jinnah", "service_type": "One Way", "airline_pnr": "4ONEWY-4AAAAA", "adult_price": "95000",
               "min_departure_date": "2026-12-01", "open_seats_slot": 30001,
               "sectors": [{**SKY_SECTOR, "sector_type": "Out", "flight_num": "740", "departure": "LHE", "destination": "DMM", "departure_date": "2026-12-01",
                            "departure_time": "08:00", "arrival_date": "2026-12-01", "arrival_time": "10:30:00"}]}
SKY_RETURN = {"id": 12067, "name": "Fly Jinnah", "service_type": "Two Way", "airline_pnr": "4YB8HL-4VK1MF", "adult_price": "178000",
              "min_departure_date": "2026-12-02", "open_seats_slot": 30334,
              "sectors": [{**SKY_SECTOR, "sector_type": "Out", "flight_num": "700", "departure": "ISB", "destination": "JED", "departure_date": "2026-12-02",
                           "departure_time": "06:40", "arrival_date": "2026-12-02", "arrival_time": "10:05:00"},
                          {**SKY_SECTOR, "sector_type": "In", "flight_num": "701", "departure": "JED", "destination": "ISB", "departure_date": "2026-12-21",
                           "departure_time": "11:05", "arrival_date": "2026-12-21", "arrival_time": "18:05:00", "baggage": "30+10"}]}


def ds_part(id_, sector, flight, day, dep, arr):
    return {"id": id_, "group_id": 3056, "departure_flight_number": flight, "departure_sector_name": sector, "departure_baggage": "20",
            "departure_handcarry": "7", "departure_date": day, "departure_time": dep, "arivval_date": day, "arivval_time": arr}


def ds_rows(subparts, ticket_type="1", date_="2026-12-05", sector="LHE - DMM", remaining=6):
    return [{"sector": sector, "airline": {"name": "PF", "logo": None}, "tickets": [{
        "pnr": f"PNR{len(subparts)}", "date": date_, "sector": sector, "air_line": "PF", "meal": 1, "price": "85000", "updated_at": "2026-11-15T08:00:00Z",
        "ticket_type": ticket_type, "baggage": "20", "departure_handcarry": "7", "remaining": remaining, "subpart": subparts}]}]


DS_ONE_WAY = ds_rows([ds_part(1, "LHE - DMM", "PF742", "2026-12-05", "21:10", "23:45")])
DS_RETURN = ds_rows([ds_part(2, "LHE - DMM", "PF742", "2026-12-05", "21:10", "23:45"), ds_part(3, "DMM - LHE", "PF743", "2026-12-20", "01:00", "06:30")],
                    ticket_type="2")
AH_ONE_WAY = al_group(4001, "LHE-DMM")
AH_RETURN = al_group(4002, "LHE-DMM-LHE", details=[
    {"id": 81, "group_id": 4002, "sr": 1, "flight_no": "PA-870", "flight_date": "2026-12-07", "origin": "Lahore", "destination": "Dammam",
     "baggage": "20+7", "dept_time": "02:00:00", "arv_time": "04:30:00"},
    {"id": 82, "group_id": 4002, "sr": 2, "flight_no": "PA-871", "flight_date": "2026-12-27", "origin": "Dammam", "destination": "Lahore",
     "baggage": "20+7", "dept_time": "06:00:00", "arv_time": "12:15:00"},
])


@pytest.fixture(autouse=True)
def no_markup():
    with patch.object(settings, "group_ticketing_markup_percent", Decimal("0")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0):
        yield


def sky(row):
    return _normalize_ticket(SkypassTicket.model_validate(row)).tickets[0]


def legs_of(view):
    return [(leg.direction, leg.flight_number, leg.origin, leg.destination, leg.departure_date, leg.departure_time, leg.arrival_time) for leg in view.legs]


# ---------- one-way and return rows, each supplier ----------

def test_skypass_one_way_and_return():
    one = ticket_view(sky(SKY_ONE_WAY), NONE)
    assert legs_of(one) == [("outbound", "9P740", "LHE", "DMM", "2026-12-01", "08:00", "10:30")]
    back = ticket_view(sky(SKY_RETURN), NONE)
    assert back.sector == "ISB - JED" and back.departure_date == "2026-12-02"
    assert legs_of(back) == [("outbound", "9P700", "ISB", "JED", "2026-12-02", "06:40", "10:05"), ("return", "9P701", "JED", "ISB", "2026-12-21", "11:05", "18:05")]
    assert back.selling_price.amount_minor == 178_000_00  # per adult, both flights
    assert baggage_text(back.baggage, back.hand_carry) == "10+20 kg" and baggage_text(back.legs[1].baggage, back.legs[1].hand_carry) == "10+30 kg"
    with patch.object(settings, "skypass_endpoint", "https://skypass.invalid"), \
         patch.object(settings, "skypass_client_id", SecretStr("id")), patch.object(settings, "skypass_client_secret", SecretStr("secret")):
        assert ticket_view(sky(SKY_RETURN), NONE).can_hold is True  # held the same way as one-way (Skypass configured)


def test_dhillu_sahi_one_way_and_return():
    one = ticket_view(normalize_groups(DS_ONE_WAY)[0].tickets[0], NONE)
    assert legs_of(one) == [("outbound", "PF742", "LHE", "DMM", "2026-12-05", "21:10", "23:45")]
    back = ticket_view(normalize_groups(DS_RETURN)[0].tickets[0], NONE)
    assert legs_of(back) == [("outbound", "PF742", "LHE", "DMM", "2026-12-05", "21:10", "23:45"), ("return", "PF743", "DMM", "LHE", "2026-12-20", "01:00", "06:30")]
    assert back.remaining == 6 and back.selling_price.amount_minor == 85_000_00 and baggage_text(back.baggage, back.hand_carry) == "7+20 kg"


def test_al_haider_one_way_and_return():
    one = ticket_view(al_haider_normalize([AH_ONE_WAY])[0].tickets[0], NONE)
    assert [leg.direction for leg in one.legs] == ["outbound"] and one.sector == "LHE - DMM"
    back = ticket_view(al_haider_normalize([AH_RETURN])[0].tickets[0], NONE)
    assert back.sector == "LHE - DMM"  # the outbound sector, so it searches and lists like a one-way LHE - DMM
    assert legs_of(back) == [("outbound", "PA870", "LHE", "DMM", "2026-12-07", "02:00", "04:30"), ("return", "PA871", "DMM", "LHE", "2026-12-27", "06:00", "12:15")]
    assert back.remaining == 4 and baggage_text(back.baggage, back.hand_carry) == "7+20 kg"


def test_connections_are_not_mistaken_for_a_return():
    connecting = al_haider_normalize([al_group(4003, "ISB-MCT-JED", details=[
        {"id": 91, "sr": 1, "flight_no": "OV-564", "flight_date": "2026-12-08", "baggage": "20+7", "dept_time": "04:25:00", "arv_time": "06:40:00"},
        {"id": 92, "sr": 2, "flight_no": "OV-283", "flight_date": "2026-12-08", "baggage": "20+7", "dept_time": "08:40:00", "arv_time": "11:10:00"}])])[0].tickets[0]
    assert [leg.direction for leg in connecting.legs] == ["outbound", "outbound"] and connecting.sector == "ISB - MCT - JED"


# ---------- the hold keeps both flights ----------

def test_hold_itinerary_and_held_page_have_both_flights():
    segments = group_holds._itinerary(sky(SKY_RETURN))
    assert [(s["direction"], s["flight_number"], s["origin"], s["destination"], s["departure_local"], s["arrival_local"]) for s in segments] == [
        ("outbound", "9P700", "ISB", "JED", "2026-12-02T06:40", "2026-12-02T10:05"), ("return", "9P701", "JED", "ISB", "2026-12-21T11:05", "2026-12-21T18:05")]
    booking = GroupBooking("b-1", "TRV-GRP-AB12CD", "4YB8HL-4VK1MF", "skypass", "held", "not_issued", "unpaid", None, "PKR", 178_000_00, 178_000_00,
                           "12067:30334", segments, 1, 0, {})
    flight = group_holds._flight(booking)
    assert (flight.sector, flight.departure_date, flight.departure_time) == ("ISB - JED", "2026-12-02", "06:40")
    assert [(leg.direction, leg.flight_number, leg.departure_date, leg.arrival_time) for leg in flight.legs] == [
        ("outbound", "9P700", "2026-12-02", "10:05"), ("return", "9P701", "2026-12-21", "18:05")]
    from app.schemas.admin_bookings import BookingSegment
    assert [BookingSegment.model_validate(s).direction for s in segments] == ["outbound", "return"]  # the booking's Itinerary tab


def test_one_way_itinerary_keeps_its_one_flight():
    segments = group_holds._itinerary(normalize_groups(DS_ONE_WAY)[0].tickets[0])
    assert len(segments) == 1 and segments[0]["departure_local"] == "2026-12-05T21:10" and segments[0]["origin"] == "LHE"


# ---------- search with any one of From, To, date ----------

def all_groups():
    return normalize_groups(DS_RETURN) + normalize_groups(ds_rows([ds_part(5, "LHE - KWI", "PF770", "2026-12-03", "09:00", "11:00")], sector="LHE - KWI", date_="2026-12-03")) \
        + [_normalize_ticket(SkypassTicket.model_validate(SKY_RETURN)), _normalize_ticket(SkypassTicket.model_validate(SKY_ONE_WAY))]


def found(**filters):
    return [(t.sector, t.departure_date) for g in filter_group_inventory(all_groups(), **filters) for t in g.tickets]


def test_filter_with_only_from_only_to_only_date_and_all_three_sorted_by_date():
    assert found(origin="LHE") == [("LHE - DMM", "2026-12-01"), ("LHE - KWI", "2026-12-03"), ("LHE - DMM", "2026-12-05")]
    assert found(destination="JED") == [("ISB - JED", "2026-12-02")]
    assert found(departure_date=date(2026, 12, 5)) == [("LHE - DMM", "2026-12-05")]
    assert found(origin="LHE", destination="DMM", departure_date=date(2026, 12, 1)) == [("LHE - DMM", "2026-12-01")]
    assert [d for _, d in found()] == ["2026-12-01", "2026-12-02", "2026-12-03", "2026-12-05"]


SUPER_ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))


def search(body):
    app = FastAPI()
    app.include_router(group_routes.router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: SUPER_ADMIN
    with patch.object(group_routes, "fetch_all_group_inventory", AsyncMock(return_value=all_groups())):
        return TestClient(app).post("/v1/group-ticketing/search", json=body)


@pytest.mark.parametrize("body, sectors", [
    ({"origin": "LHE"}, ["LHE - DMM", "LHE - KWI", "LHE - DMM"]),
    ({"destination": "JED", "departure_date": None}, ["ISB - JED"]),
    ({"origin": "", "destination": "", "departure_date": "2026-12-03"}, ["LHE - KWI"]),
    ({"origin": "ISB", "destination": "JED", "departure_date": "2026-12-02", "adults": 2}, ["ISB - JED"]),
])
def test_search_route_with_any_one_field(body, sectors):
    response = search(body)
    assert response.status_code == 200, response.text
    tickets = [t for g in response.json()["data"] for t in g["tickets"]]
    assert [t["sector"] for t in tickets] == sectors
    assert [t["departure_date"] for t in tickets] == sorted(t["departure_date"] for t in tickets)


@pytest.mark.parametrize("body", [{}, {"origin": "", "destination": "", "departure_date": ""}, {"origin": "LHE", "destination": "LHE"}])
def test_search_needs_one_field_and_different_airports(body):
    assert search(body).status_code == 422


def test_return_ticket_lists_with_both_legs_in_the_search_answer():
    response = search({"destination": "JED"})
    [ticket] = [t for g in response.json()["data"] for t in g["tickets"]]
    assert [(leg["direction"], leg["flight_number"]) for leg in ticket["legs"]] == [("outbound", "9P700"), ("return", "9P701")]
