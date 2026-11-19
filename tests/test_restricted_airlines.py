"""D32 / B6: a restricted airline is never held or booked at a supplier, however it is asked for."""

import asyncio
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.schemas.flights import FlightSearchRequest
from app.services import flight_holds, flight_requests, live_flights, restricted_airlines
from tests.southair.test_hold import ADULT, STAFF, fake_client, hold_request
from tests.sooperfare import test_flows as sf_flows


@pytest.fixture(autouse=True)
def restricted():
    live_flights._quotes.clear()
    live_flights._sessions.clear()
    restricted_airlines._cache = None
    with patch.object(settings, "restricted_airlines", "EY, QR, MM"), patch.object(settings, "ssm_parameter_prefix", None), \
         patch.object(settings, "southair_booking_enabled", True), patch.object(settings, "sooperfare_booking_enabled", True), \
         patch.object(settings, "flight_markup_percent", Decimal("2")):
        yield
    restricted_airlines._cache = None


def restricted_quote(code="MM", operating=()):
    request = FlightSearchRequest(origin="KHI", destination="MUX", departure_date="2026-11-27")
    segment = {"origin": "KHI", "destination": "MUX", "airline_code": code, "operating_codes": list(operating), "flight_number": f"{code}112"}
    ref = live_flights.FareRef("outbound", "KHIMUX", "MM112@2026-11-27T23:00", "EB", "OLD-HASH", None, segment)
    quote = live_flights.Quote("q" * 20, "u2", request, "OLD-NLD", [ref], "PKR", 45_770, 46_700, [("ADT", 1, 45_770, 46_700)],
                               live_flights._now() + 600)
    live_flights._store_quote(quote)
    return quote


@pytest.mark.parametrize("code,operating", [("MM", ()), ("PK", ("QR",))])  # marketing, or only the operating carrier
def test_hold_on_a_restricted_airline_never_calls_the_supplier(code, operating):
    restricted_quote(code, operating)
    client = fake_client()
    save = MagicMock()
    with patch.object(flight_holds.supplier_bookings, "save_hold", save), pytest.raises(flight_holds.HoldError) as error:
        asyncio.run(flight_holds.hold(hold_request(), STAFF, client_factory=lambda: client))
    assert error.value.code == "restricted_airline" and save.call_count == 0
    assert client.search.await_count == 0 and client.fare_info.await_count == 0 and client.booking.await_count == 0


def test_request_to_book_saves_a_request_and_calls_no_supplier(request):
    restricted_quote()
    client = fake_client()
    save = MagicMock(return_value="TRV-R-ABC123")
    with patch.object(flight_requests.supplier_bookings, "save_request", save), \
         patch.object(flight_requests.flight_holds, "resolve_saved", lambda req, owner: (req, {})), \
         patch.object(flight_requests.flight_holds, "_save_people", MagicMock(return_value=[])) as people:
        view = asyncio.run(flight_requests.create(hold_request(), STAFF))
    assert view.booking_id == "TRV-R-ABC123" and view.status == "requested" and "not held or confirmed" in view.message
    assert client.search.await_count == 0 and client.booking.await_count == 0
    saved = save.call_args.kwargs
    assert saved["details"]["restricted_airline"] is True and saved["selling_total_minor"] == 46_700
    [(kind, reference, details)] = request.node.notifications
    assert kind == "restricted_request" and reference == "TRV-R-ABC123" and details["pnr"] is None
    # B23: the customer and travelers are saved to (or matched with) Customers & Travelers, by the staff user.
    args = people.call_args.args
    assert args[1] == "TRV-R-ABC123" and args[2] == STAFF.user_id and args[3] is None


def test_sooperfare_restricted_fare_is_marked_for_request_and_never_reserved():
    with patch.object(settings, "restricted_airlines", "PA"):
        restricted_airlines._cache = None
        view, _ = sf_flows.search()
        fare = view.journeys[0].flights[0].fares[0]
        assert fare.booking_mode == "request"
        priced = sf_flows.reprice(view)
        assert priced.booking_mode == "request" and priced.booking_enabled is False
        sf = sf_flows.fake_sf()
        with pytest.raises(flight_holds.HoldError) as error:
            sf_flows.hold(priced, sf)
    assert error.value.code == "restricted_airline" and sf.reservation.await_count == 0 and sf.quote.await_count == 0


def test_list_is_read_from_parameter_store_when_configured():
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": "ey; qr"}}
    with patch.object(settings, "ssm_parameter_prefix", "/travelian/gateway"), patch("boto3.client", return_value=client):
        restricted_airlines._cache = None
        assert restricted_airlines.codes() == frozenset({"EY", "QR"})
    assert client.get_parameter.call_args.kwargs["Name"] == "/travelian/gateway/flights/restricted-airlines"
