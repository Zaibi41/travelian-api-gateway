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

from tests.southair.fixtures import BOOKED_LIVE, HASH_EB, HASH_LIVE, NLD, live_fare_info, live_pnr_info, live_uat_search_data, pnr_info, search_data

STAFF = BackOfficeUser("u2", "staff", NONE)
TICKET_REF = "TICKET-REF-eyJpdiSECRET"
BOOKED = {"booking_status": 1, "pnrn": "63B55C", "time_limit": "2026-11-20 13:18 UTC", "pax_count": 1, "ticket_ref": TICKET_REF}


def fake_client(search=None, booking=None, booking_error=None, fare_info=None, pnr=None):
    client = MagicMock()
    # The supplier's record of the new PNR (UAT shape); by default its total matches the guide fare.
    client.pnr_info = AsyncMock(return_value=pnr if pnr is not None else pnr_info(total=457.70, tickets=(None,)))
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


ADULT = {"type": "ADT", "title": "MR", "first_name": "Test", "last_name": "Passenger", "date_of_birth": "1990-01-01", "id_type": "PSPT",
         "document_number": "AB1234567", "document_country": "PAK", "document_expiry": "2031-01-01", "nationality": "PAK"}
INFANT = {"type": "INF", "title": "MISS", "first_name": "Baby", "last_name": "Passenger", "date_of_birth": "2026-01-01", "id_type": "PSPT",
          "document_number": "CD7654321", "document_country": "PAK", "document_expiry": "2031-01-01", "nationality": "PAK"}


def hold_request(total=46_700, passengers=None, accept=False):
    return HoldRequest(quote_id="q" * 20, confirmed_total={"amount_minor": total, "currency": "PKR"},
                       contact={"first_name": "Test", "last_name": "Customer", "email": "ops@example.invalid", "phone_code": "92", "phone": "3001234567"},
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
    assert client.fare_info.await_count == 1  # the reprice; after booking the PNR record verifies the total
    assert client.pnr_info.await_args.args == ("63B55C",)
    saved = save.call_args.kwargs
    assert saved["supplier_ref"] == TICKET_REF and saved["pnr"] == "63B55C" and saved["provider"] == "southair"
    assert saved["selling_total_minor"] == 46_700 and saved["supplier_total_minor"] == 45_770
    assert saved["audit"]["fare_info"] == "matches" and saved["audit"]["total_source"] == "pnr_info" and "first_name" not in str(saved["audit"])


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
    client = fake_client(fare_info={"ADT": {"TOTAL": "457.70"}, "INF": {"TOTAL": "50.00"}}, pnr=pnr_info(total=507.70))
    run(hold_request(total=46_700 + 5_100, passengers=[ADULT, INFANT]), client)
    infant = client.booking.await_args.kwargs["passengers"][1]
    assert infant["type"] == "INF" and infant["infant_link_id"] == "1"  # adults are numbered from 1 (UAT)


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
    client.pnr_info = AsyncMock(side_effect=SouthairError("pnr_lookup_failed"))  # no PNR record: fall back to fare_info
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


def test_live_shape_hold_uses_final_total_before_booking_and_the_pnr_record_after():
    make_live_quote()
    client = fake_client(search=live_uat_search_data(), fare_info=live_fare_info(13330), pnr=live_pnr_info(total=13330, pnr="63B55C"))
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


def test_holds_need_no_identity_storage_settings():
    from app.core.config import Settings
    assert not [name for name in Settings.model_fields if "pii" in name]
    make_quote()
    view, save = run(hold_request(), fake_client())
    assert view.pnr == "63B55C" and save.call_count == 1


def test_hold_saves_the_customer_and_travelers_with_infant_link():
    make_quote(adults=1, infants=1, selling=46_700 + 5_100)
    client = fake_client(fare_info={"ADT": {"TOTAL": "457.70"}, "INF": {"TOTAL": "50.00"}}, pnr=pnr_info(total=507.70))
    _, save = run(hold_request(total=46_700 + 5_100, passengers=[ADULT, INFANT]), client)
    kwargs = save.call_args.kwargs
    assert (kwargs["customer"].first_name, kwargs["customer"].email) == ("Test", "ops@example.invalid")
    adult, infant = kwargs["travelers"]
    assert (adult.passenger_type, adult.document_number, adult.infant_of_position) == ("ADT", "AB1234567", None)
    assert (infant.passenger_type, infant.infant_of_position) == ("INF", 0)
    assert "AB1234567" not in repr(kwargs["travelers"]) and "1990" not in str(kwargs["audit"])


@pytest.mark.parametrize("change,message", [
    ({"document_number": None}, "passport number"),
    ({"document_country": None}, "issuing country"),
    ({"document_expiry": None}, "passport expiry"),
])
def test_passport_needs_number_country_and_expiry(change, message):
    with pytest.raises(ValueError, match=message):
        hold_request(passengers=[{**ADULT, **change}])


@pytest.mark.parametrize("passenger,ok", [
    ({"id_type": "NATI", "nationality": "PAK", "document_number": "3520212345671"}, True),
    ({"id_type": "NATI", "nationality": "PAK", "document_number": "352021234567"}, False),   # 12 digits
    ({"id_type": "NATI", "nationality": "PAK", "document_number": "35202A2345671"}, False),
    ({"id_type": "NATI", "nationality": "PAK", "document_number": None}, False),
    ({"id_type": "NATI", "nationality": "GBR", "document_number": "X1234"}, True),          # only Pakistani IDs are checked
    ({"id_type": "NATI", "nationality": None, "document_number": None}, True),
])
def test_cnic_is_13_digits_only_for_pakistani_nationals(passenger, ok):
    data = {**ADULT, "document_country": None, "document_expiry": None, **passenger}
    if ok:
        hold_request(passengers=[data])
    else:
        with pytest.raises(ValueError, match="13 digits"):
            hold_request(passengers=[data])


def quote_for(origin, destination, departure="2026-11-27", return_date=None):
    request = FlightSearchRequest(origin=origin, destination=destination, departure_date=departure, return_date=return_date, adults=1)
    ref = live_flights.FareRef("outbound", f"{origin}{destination}", "MM112@2026-11-27T23:00", "EB", "OLD-HASH", None)
    live_flights._store_quote(live_flights.Quote("q" * 20, "u2", request, "OLD-NLD", [ref], "PKR", 45_770, 46_700, [], live_flights._now() + 600))
    return request


@pytest.mark.parametrize("expiry,return_date", [("2026-11-26", None), ("2026-11-29", "2026-11-30")])
def test_passport_expiring_before_the_last_travel_date_is_refused_before_booking(expiry, return_date):
    quote_for("KHI", "MUX", return_date=return_date)
    client = fake_client()
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(passengers=[{**ADULT, "document_expiry": expiry}]), client)
    assert error.value.status == 422 and "expires before the last travel date" in error.value.message
    assert client.booking.await_count == 0 and client.search.await_count == 0


@pytest.mark.parametrize("origin,destination,expiry,warned", [
    ("KHI", "DXB", "2027-03-01", True),    # international, under 6 months after 27 Nov 2026
    ("KHI", "DXB", "2027-05-27", False),   # exactly 6 months: fine
    ("DXB", "LHE", "2027-01-15", True),
    ("KHI", "MUX", "2027-01-15", False),   # domestic: no warning
    ("KHI", "MUX", "2026-11-27", False),   # expires on the travel day: allowed
])
def test_short_validity_on_an_international_route_is_a_warning_not_an_error(origin, destination, expiry, warned):
    search = FlightSearchRequest(origin=origin, destination=destination, departure_date="2026-11-27", adults=1)
    warnings = flight_holds.passport_warnings(hold_request(passengers=[{**ADULT, "document_expiry": expiry}]), search)
    assert warnings == (["Test's passport is valid for less than 6 months after travel. Check the destination's entry rules."] if warned else [])


def test_warnings_come_back_with_the_hold(monkeypatch):
    make_quote()
    monkeypatch.setattr(flight_holds, "is_domestic", lambda *codes: False)  # treat KHI-MUX as international
    view, _ = run(hold_request(passengers=[{**ADULT, "document_expiry": "2027-02-01"}]), fake_client())
    assert view.pnr == "63B55C" and len(view.warnings) == 1


def test_month_arithmetic_handles_month_ends():
    from datetime import date
    assert flight_holds._add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert flight_holds._add_months(date(2027, 8, 31), 6) == date(2028, 2, 29)
    assert flight_holds._add_months(date(2026, 11, 27), 6) == date(2027, 5, 27)


def test_each_infant_is_linked_to_its_adult_numbered_from_one():
    request = hold_request(passengers=[ADULT, {**ADULT, "first_name": "Second"}, INFANT, {**INFANT, "first_name": "Other"}])
    rows = flight_holds._supplier_passengers(request, datetime(2026, 11, 27).date())
    assert [r.get("infant_link_id") for r in rows] == [None, None, "1", "2"]


def test_an_infant_can_travel_with_the_chosen_adult():
    request = hold_request(passengers=[ADULT, {**ADULT, "first_name": "Second"}, {**INFANT, "travels_with": 1}])
    rows = flight_holds._supplier_passengers(request, datetime(2026, 11, 27).date())
    assert rows[2]["infant_link_id"] == "2"
    assert flight_holds._travelers(request)[2].infant_of_position == 1


@pytest.mark.parametrize("passengers,message", [
    ([ADULT, {**INFANT, "travels_with": 1}], "isn't on this booking"),
    ([ADULT, {**ADULT, "first_name": "Second"}, {**INFANT, "travels_with": 0}, {**INFANT, "first_name": "Other", "travels_with": 0}], "its own adult"),
])
def test_infant_adult_choice_is_checked(passengers, message):
    with pytest.raises(flight_holds.HoldError) as error:
        flight_holds._supplier_passengers(hold_request(passengers=passengers), datetime(2026, 11, 27).date())
    assert error.value.status == 422 and message in error.value.message


def test_only_infants_travel_with_an_adult():
    with pytest.raises(ValueError, match="only an infant"):
        hold_request(passengers=[{**ADULT, "travels_with": 0}])


def test_hold_errors_carry_a_code_and_the_hold_facts_over_http(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes.live_ticketing import router
    from app.core.field_visibility import require_back_office
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_back_office] = lambda: STAFF
    body = hold_request().model_dump(mode="json")
    for error, expected in (
        (flight_holds.HoldError(409, "Booking is turned off. Nothing was booked.", code="booking_off"), {"code": "booking_off"}),
        (flight_holds.HoldError(502, "The hold was created ... couldn't be verified.", code="total_unverified", pnr="63B55C", booking_id="TRV-ABC123"),
         {"code": "total_unverified", "pnr": "63B55C", "booking_id": "TRV-ABC123"}),
        (flight_holds.HoldError(500, "... couldn't be saved. Record this PNR.", code="not_saved", pnr="63B55C"), {"code": "not_saved", "pnr": "63B55C"}),
    ):
        async def hold(request, viewer, error=error):
            raise error
        monkeypatch.setattr(flight_holds, "hold", hold)
        response = TestClient(app).post("/v1/live-ticketing/flights/bookings", json=body)
        assert response.status_code == error.status and response.json() == {"detail": error.message, **expected}


def test_codes_from_the_hold_flow():
    make_quote()
    with patch.object(settings, "southair_booking_enabled", False), pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(), fake_client())
    assert error.value.code == "booking_off"
    make_quote()
    client = fake_client(search=search_data(total="500.00"), fare_info={"ADT": {"TOTAL": "500.00"}})
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(), client)
    assert error.value.code == "price_changed"
    make_quote()
    client = fake_client(booking={"booking_status": 0})
    with pytest.raises(flight_holds.HoldError) as error:
        run(hold_request(), client, saved=MagicMock())
    assert error.value.code == "not_confirmed_hold"


# ---- After booking: the PNR record verifies the total (live UAT 9 Nov 2026, PNR 5A5282, 1 adult) ----

def live_5a5282_quote():
    """The hold's quote: 1 adult, supplier Rs 14,390, selling Rs 28,780 (100% markup in that environment)."""
    request = FlightSearchRequest(origin="LHE", destination="KHI", departure_date="2026-11-19", adults=1)
    ref = live_flights.FareRef("outbound", "LHEKHI", "Z8266@2026-11-19T00:25", "EB", "OLD-HASH", None)
    live_flights._store_quote(live_flights.Quote("q" * 20, "u2", request, "OLD-NLD", [ref], "PKR", 1_439_000, 2_878_000, [], live_flights._now() + 600))


def live_5a5282_client(pnr=None, pnr_error=None, after_booking_fare_info=SouthairError("fare_info_failed")):
    search = live_uat_search_data()
    search["result"]["LHE-KHI"]["flights"]["6af1bc80e37bf"]["AVB"]["CABIN"]["EB"]["ACC"] = 14390
    client = fake_client(search=search, booking=BOOKED_LIVE)
    client.pnr_info = AsyncMock(return_value=pnr if pnr is not None else live_pnr_info(), side_effect=pnr_error)
    client.fare_info = AsyncMock(side_effect=[live_fare_info(14390), after_booking_fare_info])
    return client


def test_this_hold_verifies_from_the_pnr_record_when_fare_info_fails_after_booking():
    live_5a5282_quote()
    client = live_5a5282_client()
    with patch.object(settings, "flight_markup_percent", Decimal("100")):
        view, save = run(hold_request(total=2_878_000), client)
    assert view.pnr == "5A5282" and view.selling_total.amount_minor == 2_878_000
    assert view.time_limit == datetime(2026, 11, 10, 18, 14, 9, tzinfo=timezone.utc)
    audit, saved = save.call_args.kwargs["audit"], save.call_args.kwargs
    assert audit["fare_info"] == "matches" and audit["total_source"] == "pnr_info" and "fare_info_error" not in audit
    assert saved["supplier_total_minor"] == 1_439_000
    assert saved["supplier_response"]["pnr_info"] == {"status": "BK", "total_minor": 1_439_000, "currency": "PKR"}
    assert client.fare_info.await_count == 1  # not needed after booking


@pytest.mark.parametrize("pnr,after,status,source", [
    (live_pnr_info(total=14500), None, "differs", "pnr_info"),                               # a real mismatch
    (live_pnr_info(total=None), SouthairError("fare_info_failed"), "failed", None),           # no total anywhere
    (live_pnr_info(total=None), {"ADT": {"BASE": {"FARE": {"VALUE": 1}}}}, "no_total", None),  # fare_info without a total
    (live_pnr_info(status="XX"), SouthairError("fare_info_failed"), "failed", None),         # not booked: its total isn't used
    (live_pnr_info(currency="USD"), SouthairError("fare_info_failed"), "failed", None),      # another currency isn't compared
    (live_pnr_info(total=None), live_fare_info(14390), "matches", "fare_info"),               # fallback verifies
])
def test_unverified_or_mismatched_totals_are_never_confirmed(pnr, after, status, source):
    live_5a5282_quote()
    client = live_5a5282_client(pnr=pnr, after_booking_fare_info=after)
    save = MagicMock(return_value="TRV-PJM32J")
    with patch.object(settings, "flight_markup_percent", Decimal("100")):
        if status == "matches":
            view, _ = run(hold_request(total=2_878_000), client, saved=save)
            assert view.pnr == "5A5282"
        else:
            with pytest.raises(flight_holds.HoldError) as error:
                run(hold_request(total=2_878_000), client, saved=save)
            assert error.value.code == "total_unverified" and error.value.extra == {"pnr": "5A5282", "booking_id": "TRV-PJM32J"}
    audit = save.call_args.kwargs["audit"]
    assert audit["fare_info"] == status and audit.get("total_source") == source


def test_pnr_lookup_failing_falls_back_and_records_the_error_codes_only():
    live_5a5282_quote()
    client = live_5a5282_client(pnr_error=SouthairError("pnr lookup_failed"))
    save = MagicMock(return_value="TRV-PJM32J")
    with patch.object(settings, "flight_markup_percent", Decimal("100")), pytest.raises(flight_holds.HoldError):
        run(hold_request(total=2_878_000), client, saved=save)
    audit = save.call_args.kwargs["audit"]
    assert audit["fare_info"] == "failed" and audit["pnr_info_error"] == "pnr lookup_failed" and audit["fare_info_error"] == "fare_info_failed"
    assert "Test" not in str(audit) and "AB1234567" not in str(audit)
