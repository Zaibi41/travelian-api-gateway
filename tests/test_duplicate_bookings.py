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


def passenger(first="Ali", last="Raza", dob=date(1988, 3, 14), document="AB1234567", kind="ADT"):
    return SimpleNamespace(type=kind, first_name=first, last_name=last, date_of_birth=dob, document_number=document)


def check(passengers, journeys, rows, viewer=ADMIN, exclude=None):
    seen = {}

    def named(names, exclude_reference=None):
        seen.update(names=names, exclude=exclude_reference)
        return rows

    with patch.object(store, "travelers_named", named):
        seen["flags"] = asyncio.run(rule.check(passengers, journeys, viewer=viewer, exclude_reference=exclude))
    seen["warnings"] = rule.warnings_for(seen["flags"], viewer)
    return seen


# ---------- the rule ----------

def test_same_passenger_same_route_same_day_is_refused_with_the_reference_for_staff():
    with pytest.raises(DuplicateBooking) as error:
        check([passenger()], {KHI_ISB}, [existing()])
    assert error.value.status == 409 and error.value.code == "duplicate_booking" and error.value.reference == "TRV-OLD111"
    assert error.value.message == ("Ali Raza already has booking TRV-OLD111 on KHI → ISB on 10 Dec 2026. "
                                   "A second booking for the same passenger, route and date isn't allowed. Nothing was booked.")


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
        with rule.booking([passenger()], {KHI_ISB}, viewer=WEBSITE):  # the website is never refused, not even meanwhile
            with rule.booking([passenger()], {KHI_ISB}, viewer=WEBSITE):
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


@pytest.mark.parametrize("website", [False])
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
    if not website:
        assert "TRV-GRP-OLD111" in error.value.message and error.value.message.endswith("Nothing was held.")


def test_the_longest_names_keep_the_message_short_enough_for_the_admin_to_show():
    """The admin shows a gateway sentence of up to 300 characters; names are at most 25 letters each."""
    long_first, long_last = "Muhammad Abdullah Rehman", "Rehmanullah Khan Durrani"
    with pytest.raises(DuplicateBooking) as error:
        check([passenger(first=long_first, last=long_last)], {KHI_ISB},
              [existing(first=long_first, last=long_last, reference="TRV-GRP-ABCDEF")])
    assert len(error.value.message) <= 300


# ---------- security review: no enumeration, no blocking by strangers, reference only when visible ----------

@pytest.mark.parametrize("row", [
    existing(),                                                              # a staff hold
    existing(channel="website", payment="paid", status="payment_received"),  # a paid website booking
    existing(channel="website"),                                             # an unpaid website hold of the same contact
    existing(status="ticket_issued", payment="paid"),                        # ticketed
])
def test_the_website_is_never_refused_and_gets_no_warning_the_match_is_only_flagged_for_staff(row):
    """Contact details typed on the website prove nothing: the public answer never depends on another booking."""
    seen = check([passenger()], {KHI_ISB}, [row], viewer=WEBSITE)
    assert seen["warnings"] == [] and len(seen["flags"]) == 1          # nothing to show the guest; one flag for staff
    assert check([passenger()], {KHI_ISB}, [], viewer=WEBSITE)["flags"] == []


def test_the_website_goes_ahead_even_when_bookings_cannot_be_read():
    def broken(names, exclude_reference=None):
        raise RuntimeError("database down")

    with patch.object(store, "travelers_named", broken):
        assert asyncio.run(rule.check([passenger()], {KHI_ISB}, viewer=WEBSITE)) == []


def test_staff_notifications_never_carry_a_reference(request):
    flags = check([passenger()], {KHI_ISB}, [existing(status="hold_unrecorded", channel="website")], viewer=WEBSITE)["flags"]
    recorded = []
    with patch.object(store, "record_flag", lambda reference, actor, details: recorded.append((reference, actor, details))):
        asyncio.run(rule.flag_for_staff("TRV-NEW001", flags, actor=None))
    assert recorded == [("TRV-NEW001", None, {"journeys": ["KHI-ISB 2026-12-10"], "matches": 1, "existing_paid": False})]
    kind, reference, details = request.node.notifications[-1]
    assert kind == "possible_double_booking" and reference == "TRV-NEW001"   # the new booking only
    assert "TRV-OLD111" not in str(details) and "Ali Raza already has an active booking on KHI → ISB on 10 Dec 2026" in details["note"]
    assert "TRV-OLD111" not in rule.notification_note(flags)


def test_flagging_never_breaks_a_saved_booking(request):
    flags = check([passenger()], {KHI_ISB}, [existing()], viewer=WEBSITE)["flags"]

    def broken(*args):
        raise RuntimeError("database down")

    with patch.object(store, "record_flag", broken):
        asyncio.run(rule.flag_for_staff("TRV-NEW001", flags, actor=None))   # no error
    assert request.node.notifications[-1][0] == "possible_double_booking"
    asyncio.run(rule.flag_for_staff("TRV-NEW002", [], actor=None))           # nothing to flag: nothing sent
    assert request.node.notifications[-1][1] == "TRV-NEW001"


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


def test_website_flight_hold_goes_ahead_with_the_same_answer_and_staff_are_flagged(request):
    from app.core.config import settings
    from app.services import flight_holds
    from app.services.public_booking import WEBSITE_SEARCH_VIEWER
    from tests.southair import test_hold as southair

    route = ({"direction": "outbound", "origin": "KHI", "destination": "MUX", "departure_local": "2026-11-27T23:00"},)
    views = []
    for rows in ([], [existing(first="Test", last="Passenger", dob=date(1990, 1, 1), channel="staff", payment="paid", status="confirmed",
                               itinerary=route)]):
        southair.live_flights._quotes.clear()
        quote = southair.make_quote()
        southair.live_flights._store_quote(replace(quote, user_id=WEBSITE_SEARCH_VIEWER.user_id))
        client = southair.fake_client()
        flagged = []
        with patch.object(settings, "southair_booking_enabled", True), patch.object(settings, "flight_markup_percent", Decimal("2")), \
             patch.object(settings, "flight_markup_fixed_minor", 0), patch.object(store, "travelers_named", lambda names, exclude_reference=None: rows), \
             patch.object(store, "record_flag", lambda reference, actor, details: flagged.append(reference)), \
             patch.object(flight_holds.supplier_bookings, "save_hold", MagicMock(return_value="TRV-NEW002")), \
             patch.object(flight_holds, "_save_people", lambda *a, **kw: []):
            views.append(asyncio.run(then_background(flight_holds.hold(southair.hold_request(), WEBSITE_SEARCH_VIEWER,
                                                                       client_factory=lambda: client))))
        assert client.booking.await_count == 1                                # held as usual
    assert views[0].model_dump() == views[1].model_dump()                     # the guest's answer doesn't change
    assert flagged == ["TRV-NEW002"]                                           # the match is flagged on the new booking
    flags = [n for n in request.node.notifications if n[0] == "possible_double_booking"]
    assert len(flags) == 1 and "TRV-OLD111" not in str(flags[0])


def test_website_group_request_is_saved_and_flagged_not_refused():
    from app.services import group_holds
    from tests.group_ticketing import test_hold_record_order as group

    viewer = SimpleNamespace(role=group_holds.WEBSITE_ROLE, user_id=None, visibility=group.NONE)
    ticket = group.stock()[0].tickets[0]
    journey = sorted(rule.itinerary_journeys(group_holds._itinerary(ticket)))[0]
    rows = [existing(itinerary=({"direction": "outbound", "origin": journey[0], "destination": journey[1], "departure_local": journey[2]},),
                     payment="paid", status="confirmed")]
    flag = AsyncMock()
    with patch.object(group.store, "create_pending", MagicMock(return_value=("b-1", "TRV-GRP-AB12CD"))), \
         patch.object(group.flight_holds, "resolve_saved", lambda people, owner: (people, {})), \
         patch.object(group.flight_holds, "_save_people", lambda *a, **kw: []), \
         patch.object(group_holds, "read", AsyncMock(return_value="view")), patch.object(rule, "flag_for_staff", flag), \
         patch.object(store, "travelers_named", lambda names, exclude_reference=None: rows):
        assert asyncio.run(then_background(group_holds.hold(group.REQUEST, viewer, fetch=AsyncMock(return_value=group.stock()),
                                                            request_only=True))) == "view"
    assert flag.await_args.args[0] == "TRV-GRP-AB12CD" and len(flag.await_args.args[1]) == 1


async def then_background(call):
    """Await the call, then let the website's background flag work finish (asyncio.run would cancel it)."""
    result = await call
    await asyncio.gather(*list(rule._background))
    return result


@pytest.mark.parametrize("website", [True, False])
def test_website_flagging_runs_off_the_response_path_staff_flagging_inline(website):
    """The guest's response never waits for the flag work, so its timing can't reveal a match; staff flag inline."""
    started, release, finished = asyncio.Event(), asyncio.Event(), []

    async def slow_flag(reference, flags, *, actor, passenger=None):
        started.set()
        await release.wait()
        finished.append(reference)

    async def scenario():
        viewer = WEBSITE if website else ADMIN
        with patch.object(rule, "flag_for_staff", slow_flag):
            if website:
                await rule.flag("TRV-NEW001", [], viewer=viewer, actor=None)   # returns at once, even with nothing to flag
                assert finished == [] and len(rule._background) == 1        # scheduled, not awaited
                await asyncio.sleep(0)
                assert started.is_set()
                release.set()
                await asyncio.gather(*list(rule._background))
                assert finished == ["TRV-NEW001"] and rule._background == set()
            else:
                waiting = asyncio.ensure_future(rule.flag("TRV-NEW001", [], viewer=viewer, actor="u1"))
                await asyncio.sleep(0)
                assert not waiting.done()                                    # staff: awaited inline
                release.set()
                await waiting
                assert finished == ["TRV-NEW001"]

    asyncio.run(scenario())


def test_background_flag_errors_are_logged_never_raised(caplog):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    async def scenario():
        with patch.object(rule, "flag_for_staff", broken):
            await rule.flag("TRV-NEW001", [], viewer=WEBSITE, actor=None)
            await asyncio.gather(*list(rule._background))

    asyncio.run(scenario())
    assert "Background double-booking flag failed" in caplog.text
