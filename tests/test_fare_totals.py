"""travelian#113: each fare carries the selling total for every traveler searched, per passenger type as the supplier
priced it, so the website and admin never multiply a per-adult price. Real SooperFare answers (tests/sooperfare/fixtures)
and the Southair shapes in tests/southair/fixtures.py."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.field_visibility import NONE, BackOfficeUser
from app.schemas.flights import FlightSearchRequest
from app.services import live_flights
from app.vendors.flights.southair.client import SouthairError
from tests.sooperfare.test_flows import fake_sf
from tests.southair.fixtures import live_uat_search_data, search_data

STAFF = BackOfficeUser("u2", "staff", NONE)


@pytest.fixture(autouse=True)
def setup():
    live_flights._sessions.clear()
    live_flights._airlines.clear()
    live_flights._account_currency = None
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0), \
         patch.object(settings, "sooperfare_platform_fee_minor", 2000):
        yield


def southair(data=None, error=None):
    client = MagicMock()
    client.search = AsyncMock(return_value=data, side_effect=error)
    client.profile = AsyncMock(return_value={"office": {"currency": "PKR"}})
    return client


def run(request, southair_client=None, sf=None):
    down = southair(error=SouthairError("timeout"))
    return asyncio.run(live_flights.search(request, STAFF, client_factory=lambda: southair_client or down,
                                           sooperfare_factory=(lambda: sf) if sf else None))


def breakdown(fare):
    return {p.type: (p.count, p.selling_each.amount_minor) for p in fare.passenger_prices or []}


def test_sooperfare_return_two_adults_one_child_from_the_real_answer():
    # search_return_PA.json was read live for 2 adults + 1 child. Outbound "Value": adults Rs 42,780 for 2, child Rs 18,798.
    request = FlightSearchRequest(origin="KHI", destination="LHE", departure_date="2026-12-10", return_date="2026-12-15", adults=2, children=1)
    view = run(request, sf=fake_sf(search="search_return_PA"))
    outbound, back = view.journeys
    value = next(f for f in outbound.flights[0].fares if f.name == "Value")
    # Adult: 21,390 x 1.02 = 21,817.80 -> Rs 21,818, + platform fee share Rs 20 / 2 adults = Rs 21,828 (the per-adult price).
    # Child: 18,798 x 1.02 = 19,173.96 -> Rs 19,174. Total: 2 x 21,828 + 19,174 = Rs 62,830.
    assert value.selling_price.amount_minor == 2_182_800
    assert breakdown(value) == {"ADT": (2, 2_182_800), "CHD": (1, 1_917_400)}
    assert value.total_price.amount_minor == 6_283_000 and value.total_price.currency == "PKR"
    back_value = next(f for f in back.flights[0].fares if f.name == "Value")
    # Return "Value": adults Rs 46,540 for 2 (23,270 -> 23,735.40 -> 23,736 + 10), child Rs 20,208 (-> 20,612.16 -> 20,613).
    assert breakdown(back_value) == {"ADT": (2, 2_374_600), "CHD": (1, 2_061_300)}
    assert back_value.total_price.amount_minor == 2 * 2_374_600 + 2_061_300


def test_sooperfare_one_adult_total_is_the_adult_price():
    view = run(FlightSearchRequest(origin="KHI", destination="LHE", departure_date="2026-12-10"), sf=fake_sf())
    fare = view.journeys[0].flights[0].fares[0]
    assert fare.total_price.amount_minor == fare.selling_price.amount_minor == 2_251_100
    assert breakdown(fare) == {"ADT": (1, 2_251_100)}  # 22,050 x 1.02 = Rs 22,491 + Rs 20 fee


def test_sooperfare_answer_for_another_mix_gives_no_total_rather_than_a_guess():
    # The fixture prices 1 adult; a search for 3 adults + 1 infant can't be totalled from it.
    view = run(FlightSearchRequest(origin="KHI", destination="LHE", departure_date="2026-12-10", adults=3, infants=1), sf=fake_sf())
    fare = view.journeys[0].flights[0].fares[0]
    assert fare.selling_price is not None and fare.total_price is None and fare.passenger_prices is None


def test_southair_adults_only_total_is_each_adult_at_the_adult_price():
    # Guide shape: Rs 457.70 per adult -> 466.85 -> Rs 467.
    view = run(FlightSearchRequest(origin="KHI", destination="MUX", departure_date="2026-11-27", adults=3), southair(search_data()))
    guide = view.journeys[0].flights[1].fares[0]
    assert guide.selling_price.amount_minor == 46_700
    assert guide.total_price.amount_minor == 3 * 46_700 and breakdown(guide) == {"ADT": (3, 46_700)}


def test_southair_child_and_infant_use_their_own_tariff_totals():
    data = search_data()
    tariff = data["result"]["KHI-MUX"]["flights"]["1"]["CLASS"]["EB"]["TARRIF"]
    tariff["CHD"] = {"TOTAL": "300.00"}
    tariff["INF"] = {"TOTAL": "50.00"}
    view = run(FlightSearchRequest(origin="KHI", destination="MUX", departure_date="2026-11-27", adults=2, children=1, infants=1), southair(data))
    guide = next(f for fl in view.journeys[0].flights for f in fl.fares if f.name == "EB")
    # Child 300 -> 306; infant 50 -> 51. Total 2 x 467 + 306 + 51 = Rs 1,291.
    assert breakdown(guide) == {"ADT": (2, 46_700), "CHD": (1, 30_600), "INF": (1, 5_100)}
    assert guide.total_price.amount_minor == 129_100


def test_southair_live_answer_without_child_prices_gives_no_total():
    # Live UAT sends only ACC (the adult's price): a child's price is unknown at search time, so no total is invented.
    request = FlightSearchRequest(origin="LHE", destination="KHI", departure_date="2026-11-19", adults=1, children=1)
    view = run(request, southair(live_uat_search_data()))
    fares = [f for fl in view.journeys[0].flights for f in fl.fares]
    assert fares and all(f.selling_price is not None and f.total_price is None for f in fares)


def test_public_search_sends_the_total_and_no_supplier_fields():
    from app.services import public_booking

    request = FlightSearchRequest(origin="KHI", destination="LHE", departure_date="2026-12-15", return_date="2026-12-20", adults=2, children=1)
    with patch.object(live_flights, "configured_client", lambda: southair(error=SouthairError("timeout"))), \
         patch.object(live_flights.sooperfare, "configured", lambda: True), \
         patch.object(live_flights.sooperfare, "SooperfareClient", lambda: fake_sf(search="search_return_PA")):
        view = asyncio.run(public_booking.search_flights(request))
    body = view.model_dump(mode="json", exclude_none=True, exclude={"providers"})
    fare = body["journeys"][0]["flights"][0]["fares"][0]
    assert fare["total_price"]["amount_minor"] > fare["selling_price"]["amount_minor"]
    assert {p["type"] for p in fare["passenger_prices"]} == {"ADT", "CHD"}
    assert "supplier" not in fare and "supplier_cost" not in fare
