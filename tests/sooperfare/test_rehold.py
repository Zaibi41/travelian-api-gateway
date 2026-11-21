"""Staff "Re-hold at SooperFare" end to end (gateway#44): the old hold checked at SooperFare, the same fare searched and
quoted again, ONE new reservation, the new hold verified in SooperFare's booking list, and only then saved on the booking.
Real SooperFare answers (tests/sooperfare/fixtures) on a fake transport: nothing reaches SooperFare, nothing reaches a database."""

import asyncio
import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.db import booking_people, supplier_bookings
from app.db import issuance as store
from app.services import flight_fallback, live_flights
from app.services.public_booking import WEBSITE_SEARCH_VIEWER
from tests.sooperfare.test_flows import REQUEST, fake_sf, listing, load, southair_down

OLD_REF = "11111111-2222-4333-8444-555555555555"  # the expired reservation on the booking
NEW_REF = load("reservation_PA")["data"]["uuid"]
SELLING = 2_251_100  # search_PA's Value fare at 2% markup (tests/sooperfare/test_flows.py)
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def setup():
    live_flights._quotes.clear()
    live_flights._sessions.clear()
    live_flights._airlines.clear()
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0), \
         patch.object(settings, "sooperfare_booking_enabled", True):
        yield


def old_and_new(old_status="expired", new_billable="22070.00", new_status="active"):
    """SooperFare's booking list: the old reservation (old_status) and the new one the re-hold made."""
    answer = listing(billable=new_billable, status=new_status)
    old = copy.deepcopy(answer["data"][0])
    old["uuid"] = old["booking"]["uuid"] = OLD_REF
    old["booking"]["reservation_status"] = old_status
    answer["data"].insert(0, old)
    return answer


def saved_segment() -> dict:
    """The booking's saved flight: the first Value fare SooperFare offers on KHI-LHE (search_PA)."""
    view = asyncio.run(live_flights.search(REQUEST, WEBSITE_SEARCH_VIEWER, client_factory=southair_down, sooperfare_factory=lambda: fake_sf()))
    fare_id = view.journeys[0].flights[0].fares[0].id
    segment = live_flights.get_session(view.search_id, WEBSITE_SEARCH_VIEWER.user_id).fares[fare_id].segment
    live_flights._sessions.clear()
    return dict(segment)


def target(status="expired", total=SELLING):
    return store.Target(id="b1", reference="TRV-RH1", pnr="OLDPNR", provider="sooperfare", product="flight", booking_status=status,
                        issuance_status="not_issued", hold_expires_at=NOW - timedelta(hours=2), currency="PKR", total_minor=total,
                        supplier_cost_minor=2_207_000, supplier_ref=OLD_REF, paid_minor=0, traveler_types=[(0, "ADT")])


PEOPLE = booking_people.People(
    customer=booking_people.Customer("Sample", "Customer", "92", "3001234567", "ops@example.invalid"),
    travelers=[booking_people.Traveler(0, "ADT", "MR", "Sample", "Traveler", "NATI", None, "PAK", None, None, date(1990, 1, 1),
                                       "3520112345671")], events=[])


def run(sf, *, total=SELLING, accept=None, status="expired"):
    """staff_rehold for TRV-RH1 with `sf` as SooperFare. Returns (result or Rehold error, replace_hold mock, audit entries)."""
    row = {"provider_id": "sooperfare", "total_minor": total, "currency": "PKR", "itinerary": [saved_segment()],
           "supplier_response": {"search": REQUEST.model_dump(mode="json")}}
    replace = MagicMock(return_value="TRV-RH1")
    audit: list[tuple] = []
    with patch.object(store, "load", lambda reference: target(status, total)), \
         patch.object(flight_fallback, "_booking_row", lambda reference: row), \
         patch.object(booking_people, "read", lambda reference: PEOPLE), \
         patch.object(supplier_bookings, "claim_rehold", lambda reference, ref: "lock-1"), \
         patch.object(supplier_bookings, "release_rehold", lambda reference, token: None), \
         patch.object(supplier_bookings, "replace_hold", replace), \
         patch.object(flight_fallback, "_audit", lambda *entry: audit.append(entry)):
        try:
            result = asyncio.run(flight_fallback.staff_rehold("TRV-RH1", "staff-1", accept, sooperfare_factory=lambda: sf))
        except flight_fallback.Rehold as error:
            result = error
    return result, replace, audit


def test_same_price_one_new_reservation_verified_then_saved_on_the_same_booking():
    sf = fake_sf(lst=old_and_new())
    result, replace, audit = run(sf)
    assert isinstance(result, str) and result.startswith("held again: new PNR ZWHLZG")
    assert sf.reservation.await_count == 1  # one reservation, never retried
    assert sf.booking_list.await_count == 2  # the old hold checked first, the new one verified after
    replace.assert_called_once()
    reference, values = replace.call_args.args[0], replace.call_args.kwargs
    assert reference == "TRV-RH1" and values["supplier_ref"] == NEW_REF and values["pnr"] == "ZWHLZG"
    assert values["selling_total_minor"] == SELLING and values["supplier_total_minor"] == 2_207_000  # from SooperFare's own record
    assert values["supplier_response"]["booking_list"] == {"status": "active", "total_minor": 2_207_000}
    passenger = sf.reservation.await_args.args[0]["passengers"][0]
    assert passenger["first_name"] == "Sample" and passenger["nic"] == "3520112345671"
    assert [entry[2] for entry in audit] == ["rehold_by_staff"] and audit[0][1] == "staff-1"  # who; the event time is when


def test_a_new_price_is_shown_and_nothing_is_reserved_until_staff_accept_it():
    old_total = SELLING - 50_000  # the booking was priced lower: today's fare costs more
    sf = fake_sf(lst=old_and_new())
    error, replace, audit = run(sf, total=old_total)
    assert isinstance(error, flight_fallback.Rehold) and error.code == "price_changed" and error.status == 409
    assert error.extra["new_total"] == {"amount_minor": SELLING, "currency": "PKR"}
    assert error.extra["old_total"] == {"amount_minor": old_total, "currency": "PKR"}
    assert sf.reservation.await_count == 0 and not replace.called
    assert audit[0][1:3] == ("staff-1", "rehold_by_staff_refused") and audit[0][3]["code"] == "price_changed"
    # Staff accept the total they were shown: now it is held, at that total.
    sf = fake_sf(lst=old_and_new())
    result, replace, audit = run(sf, total=old_total, accept=SELLING)
    assert isinstance(result, str) and sf.reservation.await_count == 1
    assert replace.call_args.kwargs["selling_total_minor"] == SELLING and [entry[2] for entry in audit] == ["rehold_by_staff"]


@pytest.mark.parametrize("old_status", ["active", "on_hold"])
def test_never_a_second_hold_while_sooperfare_still_shows_the_old_one(old_status):
    sf = fake_sf(lst=old_and_new(old_status=old_status))
    error, replace, audit = run(sf)
    assert error.code == "still_active" and "Nothing was held" in error.message
    assert sf.reservation.await_count == 0 and sf.quote.await_count == 0 and not replace.called
    assert audit[0][2] == "rehold_by_staff_refused" and audit[0][3]["code"] == "still_active"


def test_airline_refusal_shows_sooperfares_words_and_changes_nothing():
    from app.vendors.flights.sooperfare.client import SooperfareError

    sf = fake_sf(lst=old_and_new(), resv_error=SooperfareError("refused", "Seats are not available on this flight.", status=422))
    error, replace, audit = run(sf)
    assert error.code == "rehold_failed" and error.status == 502
    assert "Seats are not available on this flight." in error.message and "Nothing was booked" in error.message
    assert sf.reservation.await_count == 1 and not replace.called
    assert [entry[2] for entry in audit] == ["rehold_by_staff_failed"]


def test_unclear_reservation_answer_is_never_retried_or_saved():
    from app.vendors.flights.sooperfare.client import SooperfareError

    sf = fake_sf(lst=old_and_new(), resv_error=SooperfareError("timeout"))
    error, replace, _ = run(sf)
    assert error.code == "rehold_failed" and "Check SooperFare before trying again" in error.message
    assert sf.reservation.await_count == 1 and not replace.called


@pytest.mark.parametrize("new_billable, new_status", [("28070.00", "active"), ("22070.00", "expired")])
def test_a_new_hold_sooperfare_doesnt_verify_is_cancelled_there_and_not_saved(new_billable, new_status):
    sf = fake_sf(lst=old_and_new(new_billable=new_billable, new_status=new_status))
    error, replace, _ = run(sf)
    assert error.code == "rehold_failed" and "couldn't be verified" in error.message
    assert not replace.called  # the booking keeps its expired status
    assert sf.cancel.await_count == 1 and sf.cancel.await_args.args[0] == NEW_REF  # never left open at SooperFare
