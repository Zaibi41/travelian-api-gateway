"""gateway#105: children and infants on group search; a price per passenger type only where the supplier sends it,
otherwise "only adult fares"; the searched party's total from the gateway (integer paisa, never an adult price used
for a child). Synthetic Skypass and Dhillu & Sahi listings."""

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import group_ticketing as routes
from app.api.routes import public as public_routes
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, require_search_viewer
from app.services import public_booking
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups
from app.vendors.group_ticketing.skypass.inventory import SkypassTicket, _normalize_ticket as normalize_ticket

SECTOR = [{"departure": "LHE", "destination": "JED", "departure_date": "2026-12-28", "departure_time": "23:00:00", "sector_type": "Out",
           "flight_code": "PA", "flight_num": "470", "baggage": "20+07"}]


def skypass(child="60000", infant="15000"):
    return normalize_ticket(SkypassTicket.model_validate({
        "id": 77, "name": "Airblue", "airline_pnr": "SKYPNR", "adult_price": "90000", "child_price": child, "infants_price": infant,
        "min_departure_date": "2026-12-28", "open_seats_slot": 3, "sectors": SECTOR}))


def dhillu():
    return normalize_groups([{"sector": "LHE - JED", "airline": {"name": "Saudia"}, "tickets": [
        {"pnr": "DSPNR", "date": "2026-12-28", "price": "80000", "remaining": 9, "subpart": [{"departure_time": "04:00"}]}]}])[0]


def admin_search(body, inventory, viewer=BackOfficeUser("u", "staff", NONE)):
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: viewer
    with patch.object(routes, "fetch_all_group_inventory", new=AsyncMock(return_value=inventory)), \
         patch.object(settings, "group_ticketing_markup_percent", Decimal("0")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0):
        return TestClient(app).post("/v1/group-ticketing/search", json={"origin": "LHE", "destination": "JED", **body})


def tickets(response):
    return {t["sector"] + ":" + str(t.get("flight_number")): t for g in response.json()["data"] for t in g["tickets"]}


def test_skypass_prices_children_and_infants_and_the_party_total():
    response = admin_search({"adults": 2, "children": 1, "infants": 1}, [skypass()])
    [ticket] = tickets(response).values()
    assert ticket["selling_price"]["amount_minor"] == 9_000_000
    assert ticket["child_selling_price"]["amount_minor"] == 6_000_000 and ticket["infant_selling_price"]["amount_minor"] == 1_500_000
    assert ticket["party_total"] == {"amount_minor": 2 * 9_000_000 + 6_000_000 + 1_500_000, "currency": "PKR"}
    assert ticket["only_adult_fares"] is False
    assert "child_supplier_cost" not in ticket  # cost only with the cost permission


def test_markup_applies_to_each_type_and_cost_only_with_permission():
    with patch.object(settings, "group_ticketing_markup_percent", Decimal("5")):
        ticket = routes.ticket_view(skypass().tickets[0], ALL, routes.Party(adults=1, children=1))
    assert ticket.child_selling_price.amount_minor == 6_300_000  # 60,000 + 5%
    assert ticket.party_total.amount_minor == 9_450_000 + 6_300_000
    assert ticket.child_supplier_cost.amount_minor == 6_000_000


@pytest.mark.parametrize("child,infant,searched", [
    ("Price on call", "15000", {"children": 1}),   # no child price
    ("60000", "", {"infants": 1}),                 # no infant price
])
def test_a_type_the_supplier_does_not_price_is_only_adult_fares_and_no_total(child, infant, searched):
    [ticket] = tickets(admin_search({"adults": 1, **searched}, [skypass(child=child, infant=infant)])).values()
    assert ticket["only_adult_fares"] is True and "party_total" not in ticket
    assert ticket["selling_price"]["amount_minor"] == 9_000_000  # the adult price is still shown


def test_a_supplier_with_one_price_is_only_adult_fares_for_children():
    [ticket] = tickets(admin_search({"adults": 1, "children": 2}, [dhillu()])).values()
    assert ticket["only_adult_fares"] is True and "child_selling_price" not in ticket and "party_total" not in ticket


def test_adults_only_search_has_the_total_and_no_flag():
    [ticket] = tickets(admin_search({"adults": 3}, [dhillu()])).values()
    assert ticket["party_total"]["amount_minor"] == 3 * 8_000_000 and "only_adult_fares" not in ticket


def dhillu_with(remaining):
    group = dhillu()
    group.tickets[0].remaining = remaining
    return group


def test_every_traveler_counts_against_the_seats_left():
    assert tickets(admin_search({"adults": 2, "children": 1, "infants": 1}, [dhillu_with(4)]))
    assert not tickets(admin_search({"adults": 2, "children": 1, "infants": 1}, [dhillu_with(3)]))


@pytest.mark.parametrize("body", [{"adults": 1, "infants": 2}, {"adults": 5, "children": 5}, {"adults": 1, "children": -1}])
def test_party_rules(body):
    assert admin_search(body, [skypass()]).status_code == 422


def test_public_search_has_the_same_prices_and_no_supplier_fields(monkeypatch):
    monkeypatch.setattr(public_booking, "cached_inventory", AsyncMock(return_value=[skypass(), dhillu()]))
    monkeypatch.setattr(settings, "group_ticketing_markup_percent", Decimal("0"))
    monkeypatch.setattr(public_booking, "check_rate", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(public_routes.router, prefix="/v1")
    response = TestClient(app).post("/v1/public/group-ticketing/search",
                                    json={"origin": "LHE", "destination": "JED", "adults": 1, "children": 1})
    found = tickets(response)
    assert len(found) == 2
    sky = next(t for t in found.values() if "child_selling_price" in t)
    assert sky["party_total"]["amount_minor"] == 15_000_000 and sky["only_adult_fares"] is False
    ds = next(t for t in found.values() if "child_selling_price" not in t)
    assert ds["only_adult_fares"] is True
    for leak in ("SKYPNR", "DSPNR", "supplier", "skypass", "60000\"", "can_hold"):
        assert leak not in response.text
