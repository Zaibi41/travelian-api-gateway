"""Issue #17: no double booking of the same passenger on the same route and date (flights and group tickets, staff and
website). The existing bookings are stubbed; no database or supplier is reached."""

import asyncio
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.field_visibility import ALL, NONE, BackOfficeUser
from app.db import duplicate_bookings as store
from app.services import duplicate_bookings as rule
from app.services.duplicate_bookings import DuplicateBooking, Person

OUT = {"direction": "outbound", "origin": "KHI", "destination": "ISB", "departure_local": "2026-12-10T09:00"}
BACK = {"direction": "return", "origin": "ISB", "destination": "KHI", "departure_local": "2026-12-20T18:30"}
KHI_ISB = ("KHI", "ISB", "2026-12-10")


def existing(first="Ali", last="Raza", dob=date(1988, 3, 14), document="AB1234567", itinerary=(OUT,), reference="TRV-OLD111", product="flight",
             status="held", payment="unpaid", channel="staff", email="ali@example.invalid", phone="923001234567"):
    return store.ExistingTraveler(reference=reference, product=product, itinerary=list(itinerary), first_name=first, last_name=last,
                                  date_of_birth=dob, document_number=document, booking_status=status, payment_status=payment,
                                  channel=channel, contact_email=email, contact_phone=phone)


ADMIN = BackOfficeUser("u1", "super_admin", ALL)
STAFF = BackOfficeUser("u2", "staff", NONE)       # no supplier access
WEBSITE = SimpleNamespace(role="website", user_id="website", visibility=NONE)
GUEST = SimpleNamespace(email=" Ali@Example.invalid ", phone_code="92", phone="3001234567")  # the guest's own contact
STRANGER = SimpleNamespace(email="someone@example.invalid", phone_code="92", phone="3330000000")


def passenger(first="Ali", last="Raza", dob=date(1988, 3, 14), document="AB1234567", kind="ADT"):
    return SimpleNamespace(type=kind, first_name=first, last_name=last, date_of_birth=dob, document_number=document)


def check(passengers, journeys, rows, viewer=ADMIN, exclude=None, contact=None):
    seen = {}

    def named(names, exclude_reference=None):
        seen.update(names=names, exclude=exclude_reference)
        return rows

    with patch.object(store, "travelers_named", named):
        seen["warnings"] = asyncio.run(rule.check(passengers, journeys, viewer=viewer, contact=contact, exclude_reference=exclude))
    return seen


# ---------- the rule ----------

def test_same_passenger_same_route_same_day_is_refused_with_the_reference_for_staff():
    with pytest.raises(DuplicateBooking) as error:
        check([passenger()], {KHI_ISB}, [existing()])
    assert error.value.status == 409 and error.value.code == "duplicate_booking" and error.value.reference == "TRV-OLD111"
    assert error.value.message == ("Ali Raza already has booking TRV-OLD111 on KHI → ISB on 10 Dec 2026. "
                                   "A second booking for the same passenger, route and date isn't allowed. Nothing was booked.")


def test_the_website_gets_a_plain_sentence_for_the_guests_own_booking():
    with pytest.raises(DuplicateBooking) as error:
        check([passenger()], {KHI_ISB}, [existing()], viewer=WEBSITE, contact=GUEST)
    assert error.value.status == 409 and error.value.reference is None and error.value.message == rule.WEBSITE_MESSAGE
    assert "TRV-" not in error.value.message and "Raza" not in error.value.message


@pytest.mark.parametrize("journeys,row", [
    ({("KHI", "ISB", "2026-12-11")}, existing()),                       # another date
    ({("KHI", "LHE", "2026-12-10")}, existing()),                       # another route
    ({("ISB", "KHI", "2026-12-10")}, existing()),                       # the opposite direction on that day
    ({KHI_ISB}, existing(first="Sara")),                                 # another passenger
    ({KHI_ISB}, existing(dob=date(1990, 1, 1), document="ZZ9999999")),   # same name, another person
    ({KHI_ISB}, existing(dob=None, document=None)),                      # same name, nothing else to compare
    ({KHI_ISB}, existing(itinerary=())),                                 # an old booking without recorded flights
])
def test_a_different_date_route_or_passenger_is_allowed(journeys, row):
    check([passenger()], journeys, [row])  # no error


@pytest.mark.parametrize("first,last", [("ali", "RAZA"), ("  Ali ", " Raza  "), ("Ali R", "aza"), ("ALI", "raza")])
def test_names_are_compared_ignoring_case_and_spaces(first, last):
    with pytest.raises(DuplicateBooking):
        check([passenger(first=first, last=last)], {KHI_ISB}, [existing()])
    seen = check([passenger(first=first, last=last)], {KHI_ISB}, [])
    assert seen["names"] == ["aliraza"]  # what the database compares against


def test_the_same_date_of_birth_or_the_same_document_identifies_the_passenger():
    with pytest.raises(DuplicateBooking):  # same date of birth, no documents
        check([passenger(document=None)], {KHI_ISB}, [existing(document=None)])
    with pytest.raises(DuplicateBooking):  # date of birth typed differently, same passport (spaces and case ignored)
        check([passenger(dob=date(1988, 4, 13), document="ab 123-4567")], {KHI_ISB}, [existing()])


@pytest.mark.parametrize("kind,dob", [("CHD", date(2018, 6, 1)), ("INF", date(2026, 2, 1))])
def test_children_and_infants_are_checked_like_adults(kind, dob):
    child = passenger(first="Baby", last="Raza", dob=dob, document=None, kind=kind)
    with pytest.raises(DuplicateBooking) as error:
        check([passenger(first="Other", dob=date(1970, 1, 1)), child], {KHI_ISB}, [existing(first="Baby", dob=dob, document=None)])
    assert "Baby Raza already has booking" in error.value.message
    check([passenger(first="Baby", last="Raza", dob=date(2019, 6, 1), document=None, kind=kind)], {KHI_ISB},
          [existing(first="Baby", dob=dob, document=None)])  # a sibling with the same name, another birthday: allowed


def test_a_round_trip_return_matches_another_booking_outbound():
    journeys = rule.search_journeys("ISB", "KHI", date(2026, 12, 1), date(2026, 12, 10))
    assert journeys == {("ISB", "KHI", "2026-12-01"), ("KHI", "ISB", "2026-12-10")}
    with pytest.raises(DuplicateBooking):
        check([passenger()], journeys, [existing()])
    with pytest.raises(DuplicateBooking):  # and the other way round: the existing booking's return
        check([passenger()], {("ISB", "KHI", "2026-12-20")}, [existing(itinerary=(OUT, BACK))])


def test_flights_and_group_bookings_are_checked_against_each_other():
    group = {"direction": "outbound", "origin": "KHI", "destination": "ISB", "airline_name": "PIA", "departure_local": "2026-12-10"}
    with pytest.raises(DuplicateBooking):
        check([passenger()], {KHI_ISB}, [existing(itinerary=(group,), product="group", reference="TRV-GRP-AB12CD")])


def test_journeys_from_stored_flights():
    connection = {**OUT, "destination": "DXB"}
    onward = {"direction": "outbound", "origin": "DXB", "destination": "ISB", "departure_local": "2026-12-11T02:00"}
    assert rule.itinerary_journeys([connection, onward]) == {KHI_ISB}  # first origin and date, last destination
    assert rule.itinerary_journeys([{"origin": "—", "destination": "ISB", "departure_local": "2026-12-10"}]) == set()
    assert rule.itinerary_journeys([{"origin": "KHI", "destination": "ISB", "departure_local": None}]) == set()
    assert rule.itinerary_journeys(None) == set()


def test_the_booking_being_re_held_is_left_out():
    assert check([passenger()], {KHI_ISB}, [], exclude="TRV-PAID01")["exclude"] == "TRV-PAID01"


def test_no_booking_is_made_when_bookings_cannot_be_read():
    def broken(names, exclude_reference=None):
        raise RuntimeError("database down")

    with patch.object(store, "travelers_named", broken), pytest.raises(DuplicateBooking) as error:
        asyncio.run(rule.check([passenger()], {KHI_ISB}, viewer=ADMIN))
    assert error.value.status == 503 and error.value.code == "duplicate_check_failed"


def test_two_identical_bookings_at_the_same_time_the_second_is_refused():
    with rule.booking([passenger()], {KHI_ISB}, viewer=ADMIN):
        with pytest.raises(DuplicateBooking) as error, rule.booking([passenger(first="ALI ")], {KHI_ISB}, viewer=STAFF):
            pass
        assert error.value.status == 409 and "already being booked" in error.value.message
        with rule.booking([passenger(first="Sara")], {KHI_ISB}, viewer=ADMIN):  # someone else: fine
            pass
        with rule.booking([passenger()], {KHI_ISB}, viewer=WEBSITE, contact=GUEST):  # a guest is never blocked by staff
            with pytest.raises(DuplicateBooking) as public, rule.booking([passenger()], {KHI_ISB}, viewer=WEBSITE, contact=GUEST):
                pass
            assert public.value.message == rule.WEBSITE_MESSAGE                     # the same guest's double click
            with rule.booking([passenger()], {KHI_ISB}, viewer=WEBSITE, contact=STRANGER):  # another guest: not blocked
                pass
    with rule.booking([passenger()], {KHI_ISB}, viewer=ADMIN):  # released once the first finished
        pass
    assert rule._in_flight == {}


def test_person_repr_has_no_identity_data():
    assert "Ali" not in repr(Person("Ali", "Raza", date(1988, 3, 14), "AB1234567"))
    assert "Ali" not in repr(existing())


# ---------- every creation path checks before any supplier call ----------

def _duplicate(names, exclude_reference=None):
    return [existing(first="Test", last="Passenger", dob=date(1990, 1, 1), itinerary=(
        {"direction": "outbound", "origin": "KHI", "destination": "MUX", "departure_local": "2026-11-27T23:00"},))]


@pytest.mark.parametrize("provider", ["southair", "sooperfare"])
def test_flight_hold_is_refused_before_the_supplier_is_called(provider):
    from app.core.config import settings
    from app.services import flight_holds, live_flights, sooperfare_holds
    from tests.southair import test_hold as southair

    southair.live_flights._quotes.clear()
    quote = southair.make_quote()
    if provider == "sooperfare":
        southair.live_flights._store_quote(replace(quote, provider=live_flights.SOOPERFARE))
    client = southair.fake_client()
    sooperfare = AsyncMock()
    save = MagicMock()
    with patch.object(settings, "southair_booking_enabled", True), patch.object(settings, "sooperfare_booking_enabled", True), \
         patch.object(live_flights, "booking_enabled", lambda provider: True), patch.object(store, "travelers_named", _duplicate), \
         patch.object(flight_holds.supplier_bookings, "save_hold", save), patch.object(sooperfare_holds, "hold", sooperfare), \
         pytest.raises(flight_holds.HoldError) as error:
        asyncio.run(flight_holds.hold(southair.hold_request(), southair.STAFF, client_factory=lambda: client))
    assert error.value.status == 409 and error.value.code == "duplicate_booking" and "TRV-OLD111" in error.value.message
    assert client.search.await_count == 0 and client.fare_info.await_count == 0 and client.booking.await_count == 0
    assert sooperfare.await_count == 0 and save.call_count == 0


def test_a_re_hold_of_the_same_booking_is_not_refused_by_itself():
    from app.core.config import settings
    from app.services import flight_holds
    from tests.southair import test_hold as southair

    southair.live_flights._quotes.clear()
    southair.make_quote()
    seen = []
    with patch.object(settings, "southair_booking_enabled", True), patch.object(settings, "flight_markup_percent", Decimal("2")), \
         patch.object(settings, "flight_markup_fixed_minor", 0), \
         patch.object(store, "travelers_named", lambda names, exclude_reference=None: seen.append(exclude_reference) or []), \
         patch.object(flight_holds, "save", MagicMock(return_value="TRV-PAID01")):
        view = asyncio.run(flight_holds.hold(southair.hold_request(), southair.STAFF, client_factory=southair.fake_client, into="TRV-PAID01"))
    assert view.booking_id == "TRV-PAID01" and seen == ["TRV-PAID01"]


def test_flight_request_is_refused_before_it_is_saved():
    from app.services import flight_holds, flight_requests
    from tests.southair import test_hold as southair

    southair.live_flights._quotes.clear()
    southair.make_quote()
    save = MagicMock()
    with patch.object(store, "travelers_named", _duplicate), patch.object(flight_requests.supplier_bookings, "save_request", save), \
         pytest.raises(flight_holds.HoldError) as error:
        asyncio.run(flight_requests.create(southair.hold_request(), southair.STAFF))
    assert error.value.status == 409 and error.value.code == "duplicate_booking" and save.call_count == 0


@pytest.mark.parametrize("website", [False, True])
def test_group_hold_is_refused_before_the_record_or_the_supplier(website):
    from app.services import group_holds
    from tests.group_ticketing import test_hold_record_order as group

    viewer = SimpleNamespace(role=group_holds.WEBSITE_ROLE, user_id=None, visibility=group.NONE) if website else group.ADMIN
    ticket = group.stock()[0].tickets[0]
    journey = sorted(rule.itinerary_journeys(group_holds._itinerary(ticket)))[0]
    rows = [existing(first="Ali", last="Raza", dob=date(1988, 3, 14), itinerary=(
        {"direction": "outbound", "origin": journey[0], "destination": journey[1], "departure_local": journey[2]},), reference="TRV-GRP-OLD111",
        email="a@example.invalid")]
    provider = group.Provider()
    create = MagicMock()
    with patch.object(group.store, "create_pending", create), \
         patch.object(group.flight_holds, "resolve_saved", lambda people, owner: (people, {})), \
         patch.object(store, "travelers_named", lambda names, exclude_reference=None: rows), \
         patch.dict(group_holds.PROVIDERS, {"skypass": lambda: provider}), pytest.raises(group_holds.GroupHoldError) as error:
        asyncio.run(group_holds.hold(group.REQUEST, viewer, fetch=AsyncMock(return_value=group.stock())))
    assert error.value.status == 409 and error.value.code == "duplicate_booking"
    assert create.call_count == 0 and provider.calls == []
    if website:
        assert error.value.message == rule.WEBSITE_MESSAGE
    else:
        assert "TRV-GRP-OLD111" in error.value.message and error.value.message.endswith("Nothing was held.")


def test_website_flight_checkout_shows_the_message_not_the_generic_hold_failure():
    from app.services import flight_holds, public_booking

    refused = flight_holds.HoldError(409, rule.WEBSITE_MESSAGE, code="duplicate_booking")
    body = SimpleNamespace(payment_method="book_only")
    with patch.object(flight_holds, "hold", AsyncMock(side_effect=refused)), pytest.raises(public_booking.PublicError) as error:
        asyncio.run(public_booking._hold_flight(SimpleNamespace(passengers=[]), body, SimpleNamespace(provider="southair")))
    assert (error.value.status, error.value.message, error.value.code) == (409, rule.WEBSITE_MESSAGE, "duplicate_booking")


def test_website_group_booking_route_returns_409_with_the_message():
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app
    from app.services import group_holds, public_booking
    from tests.test_public_booking import BODY

    refused = group_holds.GroupHoldError(409, rule.WEBSITE_MESSAGE, "duplicate_booking")
    with patch.object(settings, "public_group_booking_enabled", False), \
         patch.object(public_booking.group_holds, "hold", AsyncMock(side_effect=refused)):
        response = TestClient(app).post("/v1/public/group-ticketing/bookings", json=BODY)
    assert response.status_code == 409
    assert response.json()["detail"] == rule.WEBSITE_MESSAGE and response.json()["code"] == "duplicate_booking"


def test_the_longest_names_keep_the_message_short_enough_for_the_admin_to_show():
    """The admin shows a gateway sentence of up to 300 characters; names are at most 25 letters each."""
    long_first, long_last = "Muhammad Abdullah Rehman", "Rehmanullah Khan Durrani"
    with pytest.raises(DuplicateBooking) as error:
        check([passenger(first=long_first, last=long_last)], {KHI_ISB},
              [existing(first=long_first, last=long_last, reference="TRV-GRP-ABCDEF")])
    assert len(error.value.message) <= 300


# ---------- security review: no enumeration, no blocking by strangers, reference only when visible ----------

@pytest.mark.parametrize("row", [
    existing(),                                                              # a staff hold for someone else's contact
    existing(channel="website", payment="paid", status="payment_received"),  # someone else's paid website booking
    existing(email=None, phone=None),                                        # no contact recorded
])
def test_the_website_is_never_refused_by_another_customers_booking(row):
    """Same answer as when nobody is booked: the website can't be used to learn whether someone else travels."""
    assert check([passenger()], {KHI_ISB}, [row], viewer=WEBSITE, contact=STRANGER)["warnings"] == []
    assert check([passenger()], {KHI_ISB}, [], viewer=WEBSITE, contact=STRANGER)["warnings"] == []


@pytest.mark.parametrize("contact", [GUEST, SimpleNamespace(email="other@example.invalid", phone_code="+92", phone="300 1234567"),
                                     SimpleNamespace(email="ALI@example.invalid", phone_code=None, phone=None)])
def test_the_website_is_refused_by_the_same_contacts_own_booking_by_email_or_phone(contact):
    with pytest.raises(DuplicateBooking) as error:
        check([passenger()], {KHI_ISB}, [existing(channel="website")], viewer=WEBSITE, contact=contact)
    assert error.value.message == rule.WEBSITE_MESSAGE and error.value.reference is None


@pytest.mark.parametrize("row,blocks", [
    (existing(channel="staff"), True),                                                      # staff-made hold
    (existing(channel="website", payment="paid"), True),                                     # paid website booking
    (existing(channel="website", status="confirmed"), True),
    (existing(channel="website", status="ticket_issued", payment="paid"), True),
    (existing(channel="website", status="paid_not_ticketed", payment="paid"), True),
    (existing(channel="website"), False),                                                    # unpaid website hold: warning
    (existing(channel="website", status="pending_payment"), False),
])
def test_staff_are_refused_by_paid_or_staff_bookings_and_warned_about_unpaid_website_holds(row, blocks):
    if blocks:
        with pytest.raises(DuplicateBooking):
            check([passenger()], {KHI_ISB}, [row], viewer=STAFF)
        return
    warnings = check([passenger()], {KHI_ISB}, [row], viewer=ADMIN)["warnings"]
    assert warnings == ["Possible double booking: Ali Raza has an unpaid website hold (TRV-OLD111) on KHI → ISB on 10 Dec 2026. "
                        "Check with the customer that it isn't the same trip before taking payment."]


def test_a_stranger_typing_a_name_on_the_website_cannot_block_staff():
    warnings = check([passenger()], {KHI_ISB}, [existing(channel="website", email="someone@example.invalid", phone="923330000000")],
                     viewer=STAFF)["warnings"]
    assert len(warnings) == 1 and warnings[0].startswith("Possible double booking")


def test_the_reference_is_named_only_when_the_viewer_may_see_that_booking():
    unrecorded = existing(status="hold_unrecorded")
    with pytest.raises(DuplicateBooking) as hidden:           # staff without supplier access don't see unrecorded holds
        check([passenger()], {KHI_ISB}, [unrecorded], viewer=STAFF)
    assert "TRV-" not in hidden.value.message and hidden.value.reference is None
    assert hidden.value.message.startswith("Ali Raza already has a booking on KHI → ISB on 10 Dec 2026.")
    with pytest.raises(DuplicateBooking) as shown:            # super admin (supplier access) does
        check([passenger()], {KHI_ISB}, [unrecorded], viewer=ADMIN)
    assert shown.value.reference == "TRV-OLD111"
    with pytest.raises(DuplicateBooking) as staff:            # an ordinary hold: staff see it in Bookings
        check([passenger()], {KHI_ISB}, [existing()], viewer=STAFF)
    assert staff.value.reference == "TRV-OLD111"
    agency = BackOfficeUser("u3", "agency_owner", NONE)        # not back office: never a reference
    with pytest.raises(DuplicateBooking) as other:
        check([passenger()], {KHI_ISB}, [existing()], viewer=agency)
    assert "TRV-" not in other.value.message
    hidden_warning = check([passenger()], {KHI_ISB}, [existing(channel="website", status="hold_unrecorded")], viewer=STAFF)["warnings"]
    assert hidden_warning and "TRV-" not in hidden_warning[0]


def test_staff_flight_hold_carries_the_warning_and_still_holds():
    from app.core.config import settings
    from app.services import flight_holds
    from tests.southair import test_hold as southair

    southair.live_flights._quotes.clear()
    southair.make_quote()
    row = existing(first="Test", last="Passenger", dob=date(1990, 1, 1), channel="website", itinerary=(
        {"direction": "outbound", "origin": "KHI", "destination": "MUX", "departure_local": "2026-11-27T23:00"},))
    with patch.object(settings, "southair_booking_enabled", True), patch.object(settings, "flight_markup_percent", Decimal("2")), \
         patch.object(settings, "flight_markup_fixed_minor", 0), patch.object(store, "travelers_named", lambda names, exclude_reference=None: [row]), \
         patch.object(flight_holds.supplier_bookings, "save_hold", MagicMock(return_value="TRV-NEW001")):
        view = asyncio.run(flight_holds.hold(southair.hold_request(), southair.STAFF, client_factory=southair.fake_client))
    assert view.booking_id == "TRV-NEW001" and any(w.startswith("Possible double booking: Test Passenger") for w in view.warnings)


def test_website_flight_hold_ignores_a_strangers_booking_and_goes_ahead():
    from app.core.config import settings
    from app.services import flight_holds
    from app.services.public_booking import WEBSITE_SEARCH_VIEWER
    from tests.southair import test_hold as southair

    southair.live_flights._quotes.clear()
    quote = southair.make_quote()
    southair.live_flights._store_quote(replace(quote, user_id=WEBSITE_SEARCH_VIEWER.user_id))
    row = existing(first="Test", last="Passenger", dob=date(1990, 1, 1), channel="staff", payment="paid", status="confirmed",
                   email="someone@example.invalid", phone="923330000000", itinerary=(
        {"direction": "outbound", "origin": "KHI", "destination": "MUX", "departure_local": "2026-11-27T23:00"},))
    with patch.object(settings, "southair_booking_enabled", True), patch.object(settings, "flight_markup_percent", Decimal("2")), \
         patch.object(settings, "flight_markup_fixed_minor", 0), patch.object(store, "travelers_named", lambda names, exclude_reference=None: [row]), \
         patch.object(flight_holds.supplier_bookings, "save_hold", MagicMock(return_value="TRV-NEW002")), \
         patch.object(flight_holds, "_save_people", lambda *a, **kw: []):
        view = asyncio.run(flight_holds.hold(southair.hold_request(), WEBSITE_SEARCH_VIEWER, client_factory=southair.fake_client))
    assert view.booking_id == "TRV-NEW002" and view.warnings == []  # the request's contact (ops@example.invalid, 92 3001234567) isn't the row's
    # The same guest (same phone) booking the same traveler again is refused, before any supplier call.
    own = replace(row, contact_phone="923001234567")
    client = southair.fake_client()
    southair.live_flights._store_quote(replace(quote, user_id=WEBSITE_SEARCH_VIEWER.user_id))
    with patch.object(settings, "southair_booking_enabled", True), patch.object(store, "travelers_named", lambda names, exclude_reference=None: [own]), \
         pytest.raises(flight_holds.HoldError) as error:
        asyncio.run(flight_holds.hold(southair.hold_request(), WEBSITE_SEARCH_VIEWER, client_factory=lambda: client))
    assert error.value.message == rule.WEBSITE_MESSAGE and client.search.await_count == 0 and client.booking.await_count == 0
