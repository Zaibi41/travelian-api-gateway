"""SooperFare in the live flight flow: search (with Southair failing), reprice (quote + platform fee) and hold
(reservation verified against SooperFare's booking list). Real SooperFare answers (fixtures), fake transport."""

import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser
from app.schemas.flights import FlightSearchRequest
from app.schemas.live_flights import HoldRequest
from app.services import flight_holds, live_flights, sooperfare_holds
from app.vendors.flights.southair.client import SouthairError
from app.vendors.flights.sooperfare.client import SooperfareError

FIX = Path(__file__).parent / "fixtures"
STAFF = BackOfficeUser("u2", "staff", NONE)
ADMIN = BackOfficeUser("u1", "super_admin", ALL)


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def reservation(billable="22070.00"):
    answer = load("reservation_PA")
    answer["data"]["billable_price"] = billable
    return answer


def listing(billable="22070.00", status="active"):
    answer = load("booking_list")
    answer["data"][0]["booking"].update({"billable_price": billable, "reservation_status": status})
    return answer


def fake_sf(search="search_PA", quote="quote_PA", resv=None, lst=None, resv_error=None):
    client = MagicMock()
    client.availability = AsyncMock(return_value=["PA"])
    client.search = AsyncMock(return_value=load(search))
    client.quote = AsyncMock(return_value=load(quote))
    client.reservation = AsyncMock(return_value=resv or reservation(), side_effect=resv_error)
    client.booking_list = AsyncMock(return_value=lst or listing())
    return client


def southair_down():
    client = MagicMock()
    client.search = AsyncMock(side_effect=SouthairError("timeout"))
    return client


REQUEST = FlightSearchRequest(origin="KHI", destination="LHE", departure_date="2026-12-10")


@pytest.fixture(autouse=True)
def setup():
    live_flights._quotes.clear()
    live_flights._sessions.clear()
    live_flights._airlines.clear()
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0), \
         patch.object(settings, "sooperfare_booking_enabled", True), patch.object(settings, "southair_booking_enabled", False):
        yield


def search(viewer=STAFF, sf=None, request=REQUEST):
    sf = sf or fake_sf()
    return asyncio.run(live_flights.search(request, viewer, client_factory=southair_down, sooperfare_factory=lambda: sf)), sf


def test_search_merges_sooperfare_when_southair_fails_and_keeps_its_ids_inside():
    view, _ = search(ADMIN)
    assert view.status == "complete" and view.notice == "Some results may be missing."
    [outbound] = view.journeys
    assert [f.flight_number for f in outbound.flights] == ["PA400", "PA406"]
    value = outbound.flights[0].fares[0]
    assert value.name == "Value" and value.selling_price.amount_minor == 2251100 and value.supplier_cost.amount_minor == 2205000
    assert value.supplier == "sooperfare" and value.baggage == "No checked baggage, 7 kg hand carry"
    assert {p.name: p.status for p in view.providers} == {"southair": "failed", "sooperfare": "responded"}
    text = view.model_dump_json()
    assert "defd968f" not in text and "Value|EV|1" not in text and "KHILHEPAPA400" not in text  # SooperFare refs stay server side
    staff_view, _ = search(STAFF)
    assert staff_view.providers is None and staff_view.journeys[0].flights[0].fares[0].supplier is None


def reprice(view, viewer=STAFF, sf=None):
    fare_id = view.journeys[0].flights[0].fares[0].id
    return asyncio.run(live_flights.reprice(view.search_id, [fare_id], viewer, sooperfare_factory=lambda: sf or fake_sf()))


def test_reprice_quotes_and_adds_the_platform_fee_to_the_cost():
    view, _ = search(ADMIN)
    sf = fake_sf()
    priced = reprice(view, ADMIN, sf)
    assert priced.selling_total.amount_minor == 2251100 and priced.supplier_total.amount_minor == 2205000 + 2000
    assert priced.margin.amount_minor == 2251100 - 2207000 and priced.price_changed is False and priced.booking_enabled is True
    assert sf.availability.await_count == 0  # only the chosen airline is searched again
    legs = sf.quote.await_args.args[1]
    assert len(legs) == 1 and legs[0][2] == "defd968f-69fa-468e-a369-6f3a3ab13930"


def test_with_no_markup_the_platform_fee_is_passed_on_across_the_adults_so_nothing_sells_below_cost():
    # 0% markup: the Rs 20 fee per booking is in the search price already, so Book never finds it below cost.
    with patch.object(settings, "flight_markup_percent", Decimal("0")):
        view, _ = search(ADMIN)
        assert view.journeys[0].flights[0].fares[0].selling_price.amount_minor == 2205000 + 2000
        priced = reprice(view, ADMIN)
    assert priced.supplier_total.amount_minor == 2205000 + 2000
    assert priced.selling_total.amount_minor == 2205000 + 2000 and priced.margin.amount_minor == 0
    assert priced.price_changed is False and priced.booking_enabled is True
    # Several adults share it, rounded up to a whole rupee: Rs 10 each for two, Rs 7 each for three (Rs 21 >= Rs 20).
    assert live_flights.fee_share(2000, 2) == 1000 and live_flights.fee_share(2000, 3) == 700 and live_flights.fee_share(0, 2) == 0


def test_return_trip_needs_both_fares_from_the_same_offer():
    request = FlightSearchRequest(origin="KHI", destination="LHE", departure_date="2026-12-10", return_date="2026-12-15", adults=2, children=1)
    sf = fake_sf(search="search_return_PA", quote="quote_return_PA")
    view, _ = search(ADMIN, sf, request)
    outbound, back = view.journeys
    assert outbound.flights and back.flights and back.origin == "LHE"
    priced = asyncio.run(live_flights.reprice(view.search_id, [outbound.flights[0].fares[0].id, back.flights[0].fares[0].id], ADMIN,
                                              sooperfare_factory=lambda: sf))
    kinds = {p.type: p.count for p in priced.passengers}
    assert kinds == {"ADT": 2, "CHD": 1} and len(sf.quote.await_args.args[1]) == 2


ADULT = {"type": "ADT", "title": "MR", "first_name": "Test", "last_name": "Traveler", "date_of_birth": "1990-01-01", "id_type": "NATI",
         "document_number": "3520112345671", "nationality": "PAK"}


def hold(priced, sf, passengers=None, total=None):
    request = HoldRequest(quote_id=priced.quote_id, confirmed_total={"amount_minor": total or priced.selling_total.amount_minor, "currency": "PKR"},
                          contact={"first_name": "Test", "last_name": "Customer", "email": "ops@example.invalid", "phone_code": "92", "phone": "3001234567"},
                          passengers=passengers or [ADULT])
    save = MagicMock(return_value="TRV-SF1")
    with patch.object(sooperfare_holds.supplier_bookings, "save_hold", save), patch.object(flight_holds, "_save_people", MagicMock(return_value=[])):
        return asyncio.run(flight_holds.hold(request, STAFF, sooperfare_factory=lambda: sf)), save


def test_hold_reserves_once_verifies_the_total_and_saves():
    view, _ = search()
    priced = reprice(view)
    sf = fake_sf()
    result, save = hold(priced, sf)
    assert result.booking_id == "TRV-SF1" and result.pnr == "ZWHLZG" and result.label is None  # live, not "UAT test data"
    body = sf.reservation.await_args.args[0]
    passenger = body["passengers"][0]
    assert passenger["nic"] == "3520112345671" and passenger["nationality"] == "PK" and passenger["title"] == "mr" and passenger["gender"] == "male"
    assert "passport_number" not in passenger and body["contact_info"]["phone_number"] == "+923001234567"
    assert sf.reservation.await_count == 1
    saved = save.call_args.kwargs
    assert saved["provider"] == "sooperfare" and saved["supplier_ref"] == "83948930-3b71-4df4-99df-9a144a9dee3f"
    assert saved["supplier_total_minor"] == 2207000 and saved["selling_total_minor"] == 2251100 and saved["source"] is None


def test_hold_total_that_differs_is_not_saved():
    view, _ = search()
    priced = reprice(view)
    sf = fake_sf(lst=listing(billable="28070.00"))
    with pytest.raises(flight_holds.HoldError) as error:
        hold(priced, sf)
    assert error.value.code == "total_unverified" and error.value.extra["pnr"] == "ZWHLZG"


def test_refusals_before_any_reservation():
    view, _ = search()
    priced = reprice(view)
    sf = fake_sf()
    with pytest.raises(flight_holds.HoldError) as no_cnic:
        hold(priced, sf, passengers=[{**ADULT, "id_type": "PSPT", "document_number": "AB1234567", "document_country": "PAK", "document_expiry": "2031-01-01"}])
    assert no_cnic.value.code == "invalid_passengers" and sf.reservation.await_count == 0
    with patch.object(settings, "sooperfare_booking_enabled", False), pytest.raises(flight_holds.HoldError) as off:
        hold(priced, sf)
    assert off.value.code == "booking_off" and sf.reservation.await_count == 0


def test_airline_refusal_and_unclear_answer():
    view, _ = search()
    priced = reprice(view)
    refused = fake_sf(resv_error=SooperfareError("refused", "A valid phone number is required.", status=422))
    with pytest.raises(flight_holds.HoldError) as error:
        hold(priced, refused)
    assert error.value.code == "hold_refused" and "valid phone number" in error.value.message
    unclear = fake_sf(resv_error=SooperfareError("timeout"))
    with pytest.raises(flight_holds.HoldError) as error:
        hold(priced, unclear)
    assert error.value.code == "not_confirmed_hold" and unclear.reservation.await_count == 1  # never retried


def test_the_airline_list_is_reused_per_route_but_fares_are_searched_every_time():
    sf = fake_sf()
    search(sf=sf)
    search(sf=sf)
    assert sf.availability.await_count == 1 and sf.search.await_count == 2
