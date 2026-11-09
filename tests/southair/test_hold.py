"""Southair hold: flag, fresh reprice against the confirmed total, booking, fare_info, save."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser
from app.schemas.flights import FlightSearchRequest
from app.schemas.live_flights import HoldRequest
from app.services import flight_holds, live_flights
from app.vendors.flights.southair.client import SouthairError

from tests.southair.fixtures import HASH_EB, HASH_LIVE, NLD, live_fare_info, live_uat_search_data, search_data

STAFF = BackOfficeUser("u2", "staff", NONE)
TICKET_REF = "TICKET-REF-eyJpdiSECRET"
BOOKED = {"booking_status": 1, "pnrn": "63B55C", "time_limit": "2026-11-20 13:18 UTC", "pax_count": 1, "ticket_ref": TICKET_REF}


def fake_client(search=None, booking=None, booking_error=None, fare_info=None):
    client = MagicMock()
    client.search = AsyncMock(return_value=search or search_data())
    client.booking = AsyncMock(return_value=booking or BOOKED, side_effect=booking_error)
    client.fare_info = AsyncMock(return_value=fare_info if fare_info is not None else {"ADT": {"TOTAL": "457.70"}})
    client.profile = AsyncMock(return_value={"office": {"currency": "PKR"}})
    return client


def make_quote(adults=1, infants=0, selling=46_700, price_changed=False):
    request = FlightSearchRequest(origin="KHI", destination="MUX", departure_date="2026-11-27", adults=adults, infants=infants)
    ref = live_flights.FareRef("outbound", "KHIMUX", "MM112@2026-11-27T23:00", "EB", "OLD-HASH", None)
    quote = live_flights.Quote("q" * 20, "u2", request, "OLD-NLD", [ref], "PKR", 45_770, selling, [], live_flights._now() + 600,
                               price_changed=price_changed, old_total_minor=40_000 if price_changed else None)
    live_flights._store_quote(quote)
    return quote


ADULT = {"type": "ADT", "title": "MR", "first_name": "Test", "last_name": "Passenger", "date_of_birth": "1990-01-01", "id_type": "PSPT", "document_number": "AB1234567"}
INFANT = {"type": "INF", "title": "MISS", "first_name": "Baby", "last_name": "Passenger", "date_of_birth": "2026-01-01", "id_type": "PSPT"}


def hold_request(total=46_700, passengers=None, accept=False):
    return HoldRequest(quote_id="q" * 20, confirmed_total={"amount_minor": total, "currency": "PKR"},
                       contact={"email": "ops@example.invalid", "phone_code": "92", "phone": "3001234567"},
                       passengers=passengers or [ADULT], accept_price_change=accept)


@pytest.fixture(autouse=True)
def setup():
    live_flights._quotes.clear()
    live_flights._account_currency = None
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0), \
         patch.object(settings, "southair_booking_enabled", True):
        yield


def run(request, client, viewer=STAFF, saved=None):
    save = MagicMock(return_value="TRV-ABC123") if saved is None else saved
    with patch.object(flight_holds.supplier_bookings, "save_hold", save):
        return asyncio.run(flight_holds.hold(request, viewer, client_factory=lambda: client)), save


def test_hold_reprices_books_reads_fare_info_and_saves_uat_test_data():
    make_quote()
    client = fake_client()
    view, save = run(hold_request(), client)
    assert view.booking_id == "TRV-ABC123" and view.pnr == "63B55C" and view.label == "UAT test data"
    assert view.time_limit == datetime(2026, 11, 20, 13, 18, tzinfo=timezone.utc)
    assert TICKET_REF not in view.model_dump_json() and NLD not in view.model_dump_json()
    # Booking uses the fresh search's NLD and hash, not the quote's old ones.
    kwargs = client.booking.await_args.kwargs
    assert kwargs["nld"] == NLD and kwargs["acceptances"] == [HASH_EB] and kwargs["remarks"] == "Travelian UAT test booking"
    assert kwargs["passengers"][0]["passport_no"] == "AB1234567"
    assert client.fare_info.await_count == 2  # once in the reprice, once after booking
    saved = save.call_args.kwargs
    assert saved["supplier_ref"] == TICKET_REF and saved["pnr"] == "63B55C" and saved["provider"] == "southair"
    assert saved["selling_total_minor"] == 46_700 and saved["supplier_total_minor"] == 45_770
    assert saved["audit"]["fare_info"] == "matches" and "first_name" not in str(saved["audit"])


def test_flag_off_refuses_before_any_supplier_call():
    make_quote()
    client = fake_client()
    with patch.object(settings, "southair_booking_enabled", False), pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(), client)
    assert error.value.status == 409 and client.search.await_count == 0 and client.booking.await_count == 0
    assert error.value.message == "Booking is turned off. Nothing was booked."  # no env var or supplier name


def test_price_change_refuses_without_booking():
    make_quote()
    client = fake_client(search=search_data(total="500.00"), fare_info={"ADT": {"TOTAL": "500.00"}})
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(), client)
    assert error.value.status == 409 and "price changed" in error.value.message and client.booking.await_count == 0
    assert "Rs 510" in error.value.message and "PKR" not in error.value.message


@pytest.mark.parametrize("case,status", [("agency", 403), ("expired", 404), ("wrong_passengers", 422), ("bad_age", 422)])
def test_refusals(case, status):
    if case != "expired":
        make_quote()
    viewer = BackOfficeUser("u2", "agency_owner", NONE) if case == "agency" else STAFF
    passengers = [ADULT, INFANT] if case == "wrong_passengers" else [{**ADULT, "date_of_birth": "2020-01-01"}] if case == "bad_age" else None
    client = fake_client()
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(passengers=passengers), client, viewer=viewer)
    assert error.value.status == status and client.booking.await_count == 0


def test_infants_are_linked_to_adults():
    make_quote(adults=1, infants=1, selling=46_700 + 5_100)
    client = fake_client(fare_info={"ADT": {"TOTAL": "457.70"}, "INF": {"TOTAL": "50.00"}})
    run(hold_request(total=46_700 + 5_100, passengers=[ADULT, INFANT]), client)
    infant = client.booking.await_args.kwargs["passengers"][1]
    assert infant["type"] == "INF" and infant["infant_link_id"] == "0"


@pytest.mark.parametrize("failure", ["error", "not_confirmed"])
def test_supplier_failure_saves_nothing(failure):
    make_quote()
    client = fake_client(booking={"booking_status": 0}, booking_error=SouthairError("booking_failed") if failure == "error" else None)
    save = MagicMock()
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(), client, saved=save)
    assert error.value.status == 502 and save.call_count == 0 and client.booking.await_count == 1


@pytest.mark.parametrize("after,status", [(SouthairError("fare_info_failed"), "failed"), ({"ADT": {"BASE": {"FARE": {"VALUE": 450}}}}, "no_total"),
                                          ({"FINAL_TOTAL": "999.00"}, "differs")])
def test_unverified_total_after_booking_saves_the_hold_but_never_confirms_it(after, status):
    make_quote()
    client = fake_client()
    client.fare_info = AsyncMock(side_effect=[{"ADT": {"TOTAL": "457.70"}}, after])
    save = MagicMock(return_value="TRV-ABC123")
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(), client, saved=save)
    assert error.value.status == 502 and "couldn't be verified" in error.value.message
    assert "63B55C" in error.value.message and "TRV-ABC123" in error.value.message  # staff can find the hold
    assert save.call_count == 1 and save.call_args.kwargs["audit"]["fare_info"] == status


def make_live_quote():
    request = FlightSearchRequest(origin="LHE", destination="KHI", departure_date="2026-11-19", adults=1)
    ref = live_flights.FareRef("outbound", "LHEKHI", "Z8266@2026-11-19T00:25", "EB", "OLD-HASH", None)
    live_flights._store_quote(live_flights.Quote("q" * 20, "u2", request, "OLD-NLD", [ref], "PKR", 1_333_000, 1_359_700, [], live_flights._now() + 600))


def test_live_shape_hold_uses_final_total_before_and_after_booking():
    make_live_quote()
    client = fake_client(search=live_uat_search_data(), fare_info=live_fare_info(13330))
    view, save = run(hold_request(total=1_359_700), client)
    assert view.pnr == "63B55C" and view.selling_total.amount_minor == 1_359_700
    assert client.booking.await_args.kwargs["acceptances"] == [HASH_LIVE["EB"]]
    assert save.call_args.kwargs["audit"]["fare_info"] == "matches" and save.call_args.kwargs["supplier_total_minor"] == 1_333_000


@pytest.mark.parametrize("before", [live_fare_info(None), {}, SouthairError("fare_info_failed")])
def test_live_shape_no_verified_total_before_booking_creates_no_hold(before):
    make_live_quote()
    client = fake_client(search=live_uat_search_data())
    client.fare_info = AsyncMock(side_effect=[before])
    save = MagicMock()
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(total=1_359_700), client, saved=save)
    assert error.value.status == 502 and client.booking.await_count == 0 and save.call_count == 0


@pytest.mark.parametrize("minor,currency,text", [(1_399_700, "PKR", "Rs 13,997"), (51_000, "pkr", "Rs 510"), (45_770, "PKR", "Rs 457.70"), (40_000, "SAR", "SAR 400")])
def test_money_text_uses_rs_for_pkr(minor, currency, text):
    assert live_flights.money_text(minor, currency) == text


def test_changed_price_needs_explicit_acceptance_before_any_supplier_call():
    make_quote(price_changed=True)
    client = fake_client()
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(), client)
    assert error.value.status == 409 and "Accept the new total" in error.value.message and "Rs 467" in error.value.message
    assert client.search.await_count == 0 and client.booking.await_count == 0
    view, _ = run(hold_request(accept=True), client)
    assert view.pnr == "63B55C" and client.booking.await_count == 1


def test_accepted_price_that_moves_again_is_refused():
    make_quote(price_changed=True)
    client = fake_client(search=search_data(total="500.00"), fare_info={"ADT": {"TOTAL": "500.00"}})
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(accept=True), client)
    assert error.value.status == 409 and "Rs 510" in error.value.message and client.booking.await_count == 0


def test_confirmed_total_must_be_the_quotes_total():
    make_quote()
    client = fake_client()
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(total=40_000), client)
    assert error.value.status == 409 and "Rs 467" in error.value.message and client.search.await_count == 0
