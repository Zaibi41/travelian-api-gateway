"""Kyte (gokyte.com, API 3.1.28) in the live flight flow: the client (headers, errors), the Shop Flights answer ->
flights and fares, search alongside the other suppliers, and the price check before a request to book.
Answers are built to Kyte's documented schemas (ShopFlightResponse, OfferDetailsResponse); fake transport only."""

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser
from app.schemas.flights import FlightSearchRequest
from app.services import live_flights
from app.vendors.flights.kyte import client as kyte
from app.vendors.flights.kyte import normalize
from app.vendors.flights.southair.client import SouthairNotConfigured

STAFF = BackOfficeUser("u2", "staff", NONE)
ADMIN = BackOfficeUser("u1", "super_admin", ALL)
EUR = {"code": "EUR", "decimals": 2}


def segment(seg_id, carrier, name, number, origin, destination, day, dep, arr, minutes):
    return {"id": seg_id, "marketingCarrier": {"code": carrier, "name": name}, "flightNumber": number, "duration": minutes, "status": "ok",
            "departure": {"airport": {"code": origin, "name": origin}, "date": day, "time": dep},
            "arrival": {"airport": {"code": destination, "name": destination}, "date": day, "time": arr}}


def offer(offer_id, solution, price_each, fare_name, *, adults=1, children=0, single=None, currency=EUR):
    passengers = [{"id": str(i + 1), "type": "adult", "price": price_each, "priceBreakdown": []} for i in range(adults)]
    passengers += [{"id": str(adults + i + 1), "type": "child", "price": price_each - 1000, "priceBreakdown": []} for i in range(children)]
    body = {"id": offer_id, "flightSolutions": [solution], "owner": "U2", "expiration": "2026-12-01T10:00:00Z",
            "totalPrice": sum(p["price"] for p in passengers), "currency": currency, "passengers": passengers,
            "fares": {f"F-{offer_id}": {"id": f"F-{offer_id}", "name": fare_name, "segments": ["x"], "passengers": ["1"],
                                        "fareClass": "Y", "cabinType": "economy"}}}
    if single is not None:
        body["singleJourneyLeg"] = single
    return body


def shop_one_way():
    return {
        "errors": [{"code": "SEARCH_ERROR", "message": "No availability from this airline", "source": "FR"}],
        "legs": {"L1": {"id": "L1", "departureAirport": "LGW", "arrivalAirport": "BCN", "flightSolutions": ["S1", "S2"]}},
        "flightSolutions": {
            "S1": {"id": "S1", "journeyLegId": "L1", "totalDuration": 125, "segments": [
                segment("U28001-LGW-BCN", "U2", "easyJet", "8001", "LGW", "BCN", "2026-12-04", "06:10", "09:15", 125)]},
            "S2": {"id": "S2", "journeyLegId": "L1", "totalDuration": 340, "segments": [
                segment("VY7001-LGW-MAD", "VY", "Vueling Airlines", "7001", "LGW", "MAD", "2026-12-04", "10:00", "13:20", 140),
                segment("VY1003-MAD-BCN", "VY", "Vueling Airlines", "1003", "MAD", "BCN", "2026-12-04", "14:40", "15:40", 60)]},
        },
        "offers": {
            "O1": offer("O1", "S1", 6123, "Standard"),
            "O2": offer("O2", "S1", 8999, "Standard Plus"),
            "O3": offer("O3", "S2", 7500, "Basic"),
        },
    }


def shop_return():
    data = {
        "legs": {"L1": {"id": "L1", "departureAirport": "LGW", "arrivalAirport": "BCN", "flightSolutions": ["S1"]},
                 "L2": {"id": "L2", "departureAirport": "BCN", "arrivalAirport": "LGW", "flightSolutions": ["S3"]}},
        "flightSolutions": {
            "S1": {"id": "S1", "journeyLegId": "L1", "totalDuration": 125, "segments": [
                segment("U28001-LGW-BCN", "U2", "easyJet", "8001", "LGW", "BCN", "2026-12-04", "06:10", "09:15", 125)]},
            "S3": {"id": "S3", "journeyLegId": "L2", "totalDuration": 130, "segments": [
                segment("U28002-BCN-LGW", "U2", "easyJet", "8002", "BCN", "LGW", "2026-12-11", "18:00", "19:10", 130)]},
        },
        "offers": {"OUT": offer("OUT", "S1", 6123, "Standard", single=True), "BACK": offer("BACK", "S3", 5000, "Standard", single=True),
                   # an offer for both directions at once can't be shown as one outbound + one return fare: left out
                   "BOTH": {**offer("BOTH", "S1", 9000, "Bundle"), "flightSolutions": ["S1", "S3"]}},
    }
    return data


ONE_WAY = FlightSearchRequest(origin="LGW", destination="BCN", departure_date="2026-12-04")
RETURN = FlightSearchRequest(origin="LGW", destination="BCN", departure_date="2026-12-04", return_date="2026-12-11")


def fake_kyte(shop=None, details=None):
    client = MagicMock()
    client.shop = AsyncMock(return_value=shop if shop is not None else shop_one_way())
    client.offer_details = AsyncMock(return_value=details or {"details": {}})
    return client


def southair_off():
    raise SouthairNotConfigured()


@pytest.fixture(autouse=True)
def setup():
    live_flights._quotes.clear()
    live_flights._sessions.clear()
    with patch.object(settings, "flight_markup_percent", Decimal("0")), patch.object(settings, "flight_markup_fixed_minor", 0), \
         patch.object(settings, "sooperfare_base_url", None), patch.object(settings, "kyte_api_key", None):
        yield


# ---------- money and parsing ----------

def test_amounts_follow_the_currency_decimals():
    assert normalize.to_minor(6123, 2) == 6123
    assert normalize.to_minor(15000, 0) == 1_500_000     # e.g. JPY: no decimals
    assert normalize.to_minor(12345, 3) == 1235          # e.g. KWD: rounded to two decimals
    assert normalize.to_minor("12", 2) is None and normalize.to_minor(10, None) is None


def test_one_way_answer_becomes_flights_with_their_fares():
    flights = normalize.parse_shop(shop_one_way(), "LGW", "BCN", two_way=False)
    by_id = {f.signature: f for f in flights}
    easy = by_id["S1"]
    assert (easy.airline_code, easy.airline_name, easy.flight_number) == ("U2", "easyJet", "U28001")
    assert (easy.departure_local, easy.arrival_local, easy.duration_minutes, easy.stops) == ("2026-12-04T06:10", "2026-12-04T09:15", 125, 0)
    assert [(f.name, f.adult_each_minor, f.currency) for f in easy.fares] == [("Standard", 6123, "EUR"), ("Standard Plus", 8999, "EUR")]
    vueling = by_id["S2"]
    assert (vueling.stops, vueling.stop_cities, vueling.destination) == (1, ("MAD",), "BCN")
    assert not vueling.fares[0].baggage.known  # not in the search answer: never guessed


def test_return_answer_uses_one_offer_per_direction():
    flights = normalize.parse_shop(shop_return(), "LGW", "BCN", two_way=True)
    assert sorted((f.direction, f.signature, [x.offer_id for x in f.fares]) for f in flights) == [
        ("outbound", "S1", ["OUT"]), ("return", "S3", ["BACK"])]


def test_unusable_answers_give_no_flights():
    assert normalize.parse_shop(None, "LGW", "BCN", two_way=False) == []
    assert normalize.parse_shop({"legs": [], "flightSolutions": {}, "offers": {}}, "LGW", "BCN", two_way=False) == []
    broken = shop_one_way()
    broken["offers"]["O1"]["passengers"][0]["type"] = "pet"
    assert [f.offer_id for f in next(x for x in normalize.parse_shop(broken, "LGW", "BCN", two_way=False) if x.signature == "S1").fares] == ["O2"]


# ---------- client ----------

def test_client_sends_the_key_and_correlation_ids_and_maps_errors():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=shop_one_way())

    client = kyte.KyteClient("https://api.sandbox.gokyte.com", SecretStr("k-test"), 5, transport=httpx.MockTransport(handler))
    asyncio.run(client.shop({"journeys": []}, transaction_id="tx-1", airlines=["U2", "VY"]))
    request = seen[0]
    assert request.url.path == "/api/v3/flights/shop" and request.url.params.get_list("airlines") == ["U2", "VY"]
    assert request.headers["x-api-key"] == "k-test" and request.headers["x-transaction-id"] == "tx-1" and request.headers["x-request-id"]
    assert json.loads(request.content) == {"journeys": []}

    refused = kyte.KyteClient("https://api.sandbox.gokyte.com", SecretStr("k"), 5,
                              transport=httpx.MockTransport(lambda r: httpx.Response(403, text="<html>403 Forbidden</html>")))
    with pytest.raises(kyte.KyteError) as error:
        asyncio.run(refused.shop({}, transaction_id="t"))
    assert error.value.error_code == "unauthorized" and "k" not in error.value.public_message
    with pytest.raises(kyte.KyteNotConfigured):
        kyte.KyteClient("https://x.invalid", None)


def test_search_body_follows_the_request():
    body = live_flights.kyte_shop_body(FlightSearchRequest(origin="LGW", destination="BCN", departure_date="2026-12-04",
                                                           return_date="2026-12-11", adults=2, children=1, infants=1, cabin="premium_economy"))
    assert [j["date"]["main"] for j in body["journeys"]] == ["2026-12-04", "2026-12-11"]
    assert body["passengers"] == [{}, {}, {"age": 8}, {"age": 1}]
    assert (body["cabinType"], body["splitOffer"]) == ("premiumEconomy", True)


# ---------- search and price check ----------

def test_kyte_fares_appear_in_search_as_requests_with_selling_prices():
    with patch.object(settings, "flight_markup_percent", Decimal("10")):
        view = asyncio.run(live_flights.search(ONE_WAY, ADMIN, client_factory=southair_off, kyte_factory=lambda: fake_kyte()))
    flights = view.journeys[0].flights
    easy = next(f for f in flights if f.airline_code == "U2")
    fare = easy.fares[0]
    assert fare.booking_mode == "request" and fare.supplier == "kyte"
    assert (fare.supplier_cost.amount_minor, fare.supplier_cost.currency) == (6123, "EUR")
    assert fare.selling_price.amount_minor == 6800  # 61.23 + 10% = 67.36, rounded up to a whole unit
    assert {p.name: p.status for p in view.providers} == {"southair": "not_configured", "kyte": "responded"}

    staff_view = asyncio.run(live_flights.search(ONE_WAY, STAFF, client_factory=southair_off, kyte_factory=lambda: fake_kyte()))
    staff_fare = staff_view.journeys[0].flights[0].fares[0]
    assert staff_fare.supplier is None and staff_fare.supplier_cost is None  # no supplier names or costs for staff


def test_price_check_confirms_the_offer_and_keeps_it_a_request():
    fake = fake_kyte(details={"details": {"O1": {"id": "O1", "amount": 6123, "currency": EUR, "disclosures": [], "baggageAllowance": [],
                                                   "servicingFees": []}}})
    view = asyncio.run(live_flights.search(ONE_WAY, ADMIN, client_factory=southair_off, kyte_factory=lambda: fake))
    fare_id = next(f for f in view.journeys[0].flights if f.airline_code == "U2").fares[0].id
    quote = asyncio.run(live_flights.reprice(view.search_id, [fare_id], ADMIN, client_factory=southair_off, kyte_factory=lambda: fake))
    assert quote.booking_mode == "request" and quote.booking_enabled is False
    assert (quote.selling_total.amount_minor, quote.selling_total.currency) == (6200, "EUR")
    assert fake.offer_details.await_args.args[0] == ["O1"]
    # the same transaction id as the search (Kyte follows one shopping flow)
    assert fake.offer_details.await_args.kwargs["transaction_id"] == fake.shop.await_args.kwargs["transaction_id"]


def test_a_changed_or_gone_offer_is_refused():
    changed = fake_kyte(details={"details": {"O1": {"id": "O1", "amount": 7000, "currency": EUR}}})
    view = asyncio.run(live_flights.search(ONE_WAY, ADMIN, client_factory=southair_off, kyte_factory=lambda: changed))
    fare_id = next(f for f in view.journeys[0].flights if f.airline_code == "U2").fares[0].id
    with pytest.raises(live_flights.RepriceError) as error:
        asyncio.run(live_flights.reprice(view.search_id, [fare_id], ADMIN, client_factory=southair_off, kyte_factory=lambda: changed))
    assert error.value.code == "fare_gone"
    changed.offer_details = AsyncMock(return_value={"details": {}})
    with pytest.raises(live_flights.RepriceError):
        asyncio.run(live_flights.reprice(view.search_id, [fare_id], ADMIN, client_factory=southair_off, kyte_factory=lambda: changed))


def test_round_trip_quote_adds_both_directions():
    details = {"details": {"OUT": {"id": "OUT", "amount": 6123, "currency": EUR}, "BACK": {"id": "BACK", "amount": 5000, "currency": EUR}}}
    fake = fake_kyte(shop=shop_return(), details=details)
    view = asyncio.run(live_flights.search(RETURN, ADMIN, client_factory=southair_off, kyte_factory=lambda: fake))
    out, back = (view.journeys[0].flights[0].fares[0].id, view.journeys[1].flights[0].fares[0].id)
    quote = asyncio.run(live_flights.reprice(view.search_id, [out, back], ADMIN, client_factory=southair_off, kyte_factory=lambda: fake))
    assert quote.selling_total.amount_minor == 11200  # 61.23 + 50.00 = 111.23 supplier cost, rounded up to 112
    assert sorted(fake.offer_details.await_args.args[0]) == ["BACK", "OUT"]


def test_kyte_failing_leaves_the_other_results_and_a_notice():
    failing = MagicMock()
    failing.shop = AsyncMock(side_effect=kyte.KyteError("timeout"))
    view = asyncio.run(live_flights.search(ONE_WAY, ADMIN, client_factory=southair_off, kyte_factory=lambda: failing))
    assert {p.name: p.status for p in view.providers}["kyte"] == "failed"


def test_website_searches_skip_kyte_unless_switched_on():
    website = BackOfficeUser("website", "website", NONE)
    fake = fake_kyte()
    with patch.object(settings, "kyte_api_key", SecretStr("k")), patch.object(kyte, "configured_client", lambda: fake), \
         patch.object(settings, "kyte_public_enabled", False):
        asyncio.run(live_flights.search(ONE_WAY, website, client_factory=southair_off))
        assert fake.shop.await_count == 0          # sandbox fares never reach customers
        asyncio.run(live_flights.search(ONE_WAY, ADMIN, client_factory=southair_off))
        assert fake.shop.await_count == 1          # back-office searches include Kyte
    with patch.object(settings, "kyte_api_key", SecretStr("k")), patch.object(kyte, "configured_client", lambda: fake), \
         patch.object(settings, "kyte_public_enabled", True):
        asyncio.run(live_flights.search(ONE_WAY, website, client_factory=southair_off))
        assert fake.shop.await_count == 2


def test_codeshare_flight_keeps_its_own_airline_and_the_operating_airline():
    from app.services.live_flights import segment_views
    from app.vendors.flights.kyte.normalize import _segment
    sold = segment("S2", "TK", "Turkish Airlines", "8050", "IST", "AUH", "2026-12-05", "13:55", "19:25", 270)
    sold["operatingCarrier"] = {"code": "EY", "name": "Etihad Airways"}
    [view] = segment_views([_segment(sold)])
    assert (view.flight_number, view.airline_code, view.airline_name, view.operating_code, view.operating_name) == (
        "TK8050", "TK", "Turkish Airlines", "EY", "Etihad Airways")
