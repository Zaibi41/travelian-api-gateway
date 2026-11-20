"""Supplier cost, selling price and profit (Group Ticketing and Flights, one model: app.services.pricing).
Mocked suppliers and store only; the database parts are in tests/database (skipped without a test database)."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import admin_bookings as booking_routes
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, require_back_office, resolve_actions
from app.db import booking_pricing, group_holds as group_store
from app.db import issuance as issuance_store
from app.schemas.group_holds import GroupHoldRequest
from app.services import flight_holds, group_holds, issuance, live_flights
from app.services.group_holds import GroupHoldError, HeldSeats
from app.services.group_ticketing import group_ticket_id
from app.services.group_ticketing_view import GroupMoney
from app.services.pricing import Amounts, PriceRefused, check_selling, split
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups

SUPER_ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))
CASHIER = BackOfficeUser("u-2", "staff", NONE, frozenset({"record_payments"}))  # may set prices, no cost access
STAFF = BackOfficeUser("u-3", "staff", NONE)
COST_STAFF = BackOfficeUser("u-4", "staff", FieldVisibility(False, True), frozenset({"record_payments"}))


# ---------- the shared rule ----------

@pytest.mark.parametrize("selling, cost", [(88_200_00, 88_000_00), (88_000_00, 88_000_00), (90_000_00, None)])
def test_selling_above_or_equal_to_cost_is_accepted(selling, cost):
    check_selling(selling, cost)


@pytest.mark.parametrize("selling, cost, code", [(87_900_00, 88_000_00, "below_cost"), (88_200_50, 88_000_00, "invalid_selling_price"),
                                                 (0, None, "invalid_selling_price")])
def test_below_cost_or_part_rupees_is_refused(selling, cost, code):
    with pytest.raises(PriceRefused) as raised:
        check_selling(selling, cost)
    assert raised.value.code == code


def test_split_and_profit():
    assert split(176_400_00, 2) == [88_200_00, 88_200_00] and split(10, 3) == [4, 3, 3]
    assert Amounts(88_200_00, 88_000_00).profit_minor == 200_00 and not Amounts(88_200_00, 88_000_00).below_cost
    assert Amounts(88_000_00, 88_500_00).below_cost


# ---------- Group Ticketing: selling price at the hold step ----------

def rows(price="88000"):
    return [{"sector": "LHE - DMM", "airline": {"name": "PF", "logo": None}, "tickets": [{
        "pnr": "PNR1", "date": "2026-12-05", "sector": "LHE - DMM", "price": price, "remaining": 5, "updated_at": "2026-11-15T08:00:00Z",
        "subpart": [{"id": 1, "group_id": 9, "departure_flight_number": "PF742", "departure_sector_name": "LHE - DMM", "departure_time": "21:10"}]}]}]


class Provider:
    def __init__(self, held_cost=None):
        self.held_cost, self.calls = held_cost, []

    async def hold(self, ticket, passengers, contact):
        self.calls.append("hold")
        total = GroupMoney(amount_minor=self.held_cost * len(passengers), currency="PKR") if self.held_cost else None
        return HeldSeats("PNR1", "ref", GroupMoney(amount_minor=88_000_00, currency="PKR"), supplier_total=total)


@pytest.fixture
def group_store_calls():
    calls = []
    with patch.object(group_store, "create_pending", MagicMock(side_effect=lambda **kw: calls.append(("create_pending", kw)) or ("b-1", "TRV-GRP-AB12CD"))), \
         patch.object(group_store, "confirm_hold", MagicMock(side_effect=lambda *a, **kw: calls.append(("confirm_hold", kw)))), \
         patch.object(group_store, "mark_hold_failed", MagicMock()), \
         patch.object(flight_holds, "resolve_saved", lambda people, owner: (people, {})), \
         patch.object(flight_holds, "_save_people", lambda *a, **kw: []), \
         patch.object(group_holds, "read", AsyncMock(side_effect=lambda reference, viewer: MagicMock(booking_id=reference))), \
         patch.object(settings, "group_ticketing_markup_percent", Decimal("0")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0):
        yield calls


def group_hold(selling=None, viewer=SUPER_ADMIN, provider=None, adults=2):
    stock = normalize_groups(rows())
    passenger = {"type": "ADT", "title": "MR", "first_name": "Ali", "last_name": "Raza", "date_of_birth": "1990-01-01", "id_type": "PSPT",
                 "document_number": "AB1234567", "document_country": "PAK", "document_expiry": "2031-01-01", "nationality": "PAK"}
    request = GroupHoldRequest(fare_terms_acknowledged=True, ticket_id=UUID(group_ticket_id(stock[0].tickets[0])), adults=adults,
                               passengers=[passenger, {**passenger, "first_name": "Sana", "title": "MRS"}][:adults],
                               contact={"full_name": "Ali Raza", "phone": "+92 300 1234567", "email": "a@example.invalid"},
                               selling_price_per_adult=None if selling is None else {"amount_minor": selling, "currency": "PKR"})
    provider = provider or Provider()
    with patch.dict(group_holds.PROVIDERS, {"dhillu_sahi": lambda: provider}):
        asyncio.run(group_holds.hold(request, viewer, fetch=AsyncMock(return_value=stock)))
    return provider


def test_selling_above_cost_is_the_booking_total_and_cost_is_kept(group_store_calls):
    group_hold(selling=88_200_00)
    pending, confirmed = dict(group_store_calls)["create_pending"], dict(group_store_calls)["confirm_hold"]
    assert (pending["selling_total_minor"], pending["supplier_total_minor"]) == (176_400_00, 176_000_00)
    assert [(t.selling_minor, t.cost_minor) for t in pending["travelers"]] == [(88_200_00, 88_000_00)] * 2
    assert (confirmed["selling_total_minor"], confirmed["supplier_total_minor"]) == (176_400_00, 176_000_00)  # profit 400


def test_default_selling_is_the_supplier_price_plus_markup_and_equal_is_fine(group_store_calls):
    group_hold(selling=None)
    assert dict(group_store_calls)["create_pending"]["selling_total_minor"] == 176_000_00  # markup 0 when unset
    group_store_calls.clear()
    group_hold(selling=88_000_00)
    assert dict(group_store_calls)["confirm_hold"]["selling_total_minor"] == 176_000_00


@pytest.mark.parametrize("selling, status, code", [(87_999_00, 422, "below_cost"), (88_100_50, 422, "invalid_selling_price")])
def test_selling_below_cost_or_part_rupees_is_refused_before_anything_is_held(group_store_calls, selling, status, code):
    with pytest.raises(GroupHoldError) as raised:
        provider = Provider()
        group_hold(selling=selling, provider=provider)
    assert (raised.value.status, raised.value.code) == (status, code)
    assert raised.value.field_errors[0][0] == ["selling_price_per_adult"] and group_store_calls == []


def test_changing_the_selling_price_needs_record_payments(group_store_calls):
    with pytest.raises(GroupHoldError) as raised:
        group_hold(selling=90_000_00, viewer=STAFF)
    assert raised.value.status == 403 and group_store_calls == []
    group_hold(selling=None, viewer=STAFF)  # the default needs no permission
    group_hold(selling=90_000_00, viewer=CASHIER)


def test_supplier_price_change_at_hold_keeps_the_selling_price(group_store_calls):
    group_hold(selling=90_000_00, provider=Provider(held_cost=89_000_00))
    confirmed = dict(group_store_calls)["confirm_hold"]
    assert confirmed["selling_total_minor"] == 180_000_00 and confirmed["supplier_total_minor"] == 178_000_00
    assert confirmed["supplier_response"]["price_change"] == {"shown_cost_total_minor": 176_000_00, "held_cost_total_minor": 178_000_00, "accepted": False}


def held_booking(total, cost, change=None, paid=0):
    return group_store.GroupBooking("b-1", "TRV-GRP-AB12CD", "PNR1", "dhillu_sahi", "held", "not_issued", "unpaid", None, "PKR", total, cost,
                                    "ref", [], 2, paid, {}, {}, {}, change or {})


def test_price_change_view_shows_the_new_profit_and_flags_below_cost():
    change = {"shown_cost_total_minor": 176_000_00, "held_cost_total_minor": 182_000_00, "accepted": False}
    record = held_booking(180_000_00, 182_000_00, change)
    from datetime import datetime, timezone
    view = group_holds.build_view(record, None, SUPER_ADMIN, datetime.now(timezone.utc))
    assert view.price_change.below_cost is True and view.price_change.profit.amount_minor == -2_000_00
    assert view.profit.amount_minor == -2_000_00 and view.supplier_cost.amount_minor == 182_000_00
    staff = group_holds.build_view(record, None, STAFF, datetime.now(timezone.utc))
    assert staff.price_change.below_cost is True and staff.price_change.profit is None and staff.supplier_cost is None and staff.profit is None
    assert view.can_set_selling_price is True and staff.can_set_selling_price is False


# ---------- the shared selling-price endpoint (Group and Flights) ----------

def booking_client(monkeypatch, viewer, result=None, error=None):
    app = FastAPI()
    app.include_router(booking_routes.router, prefix="/v1")
    app.dependency_overrides[require_back_office] = lambda: viewer
    seen = {}

    def set_price(reference, prices, actor, currency=None):
        seen.update(reference=reference, prices=prices, currency=currency)
        if error:
            raise error
        return result

    monkeypatch.setattr(booking_pricing, "set_selling_price", set_price)
    return TestClient(app), seen


BODY = {"prices": [{"passenger_type": "ADT", "selling_each": {"amount_minor": 88_200_00, "currency": "PKR"}}]}


def test_endpoint_sets_the_price_and_shows_cost_and_profit_only_with_cost_access(monkeypatch):
    result = booking_pricing.SellingSet("TRV-GRP-AB12CD", Amounts(176_400_00, 176_000_00), "PKR")
    client, seen = booking_client(monkeypatch, SUPER_ADMIN, result)
    answer = client.post("/v1/admin/bookings/TRV-GRP-AB12CD/selling-price", json=BODY)
    assert answer.status_code == 200 and answer.json() == {"total": {"amount_minor": 176_400_00, "currency": "PKR"},
                                                           "supplier_cost": {"amount_minor": 176_000_00, "currency": "PKR"},
                                                           "profit": {"amount_minor": 400_00, "currency": "PKR"}}
    assert seen == {"reference": "TRV-GRP-AB12CD", "prices": {"ADT": 88_200_00}, "currency": "PKR"}
    client, _ = booking_client(monkeypatch, CASHIER, result)
    assert client.post("/v1/admin/bookings/TRV-GRP-AB12CD/selling-price", json=BODY).json() == {"total": {"amount_minor": 176_400_00, "currency": "PKR"}}


def test_endpoint_needs_record_payments_and_maps_refusals(monkeypatch):
    client, seen = booking_client(monkeypatch, STAFF)
    assert client.post("/v1/admin/bookings/TRV-1/selling-price", json=BODY).status_code == 403 and seen == {}
    for error in (booking_pricing.SellingRefused(422, "The selling price can't be below the supplier cost.", "below_cost", "ADT"),
                  booking_pricing.SellingRefused(409, "A payment is already recorded, so the selling price can't change.", "payment_recorded")):
        client, _ = booking_client(monkeypatch, CASHIER, error=error)
        answer = client.post("/v1/admin/bookings/TRV-1/selling-price", json=BODY)
        assert answer.status_code == error.status and answer.json()["detail"] == error.message


# ---------- Flights: airline price change at hold ----------

from tests.southair.test_hold import fake_client, hold_request  # noqa: E402
from tests.southair.fixtures import pnr_info, search_data  # noqa: E402


def flight_quote(selling_each, user_id):
    from app.schemas.flights import FlightSearchRequest
    request = FlightSearchRequest(origin="KHI", destination="MUX", departure_date="2026-11-27", adults=1)
    ref = live_flights.FareRef("outbound", "KHIMUX", "MM112@2026-11-27T23:00", "EB", "OLD-HASH", None)
    live_flights._store_quote(live_flights.Quote("q" * 20, user_id, request, "OLD-NLD", [ref], "PKR", 45_770, selling_each,
                                                 [("ADT", 1, 45_770, selling_each)], live_flights._now() + 600))


@pytest.fixture
def flights():
    live_flights._quotes.clear()
    live_flights._account_currency = None
    with patch.object(settings, "flight_markup_percent", Decimal("2")), patch.object(settings, "flight_markup_fixed_minor", 0), \
         patch.object(settings, "southair_booking_enabled", True):
        yield


def flight_hold(selling_each, new_cost_text, viewer=COST_STAFF):
    flight_quote(selling_each, viewer.user_id)
    client = fake_client(search=search_data(total=new_cost_text), pnr=pnr_info(total=float(new_cost_text), tickets=(None,)),
                         fare_info={"ADT": {"TOTAL": new_cost_text}})
    save = MagicMock(return_value="TRV-ABC123")
    with patch.object(flight_holds.supplier_bookings, "save_hold", save):
        view = asyncio.run(flight_holds.hold(hold_request(total=selling_each), viewer, client_factory=lambda: client))
    return view, save


def test_flight_cost_up_keeps_the_selling_price_and_shows_the_new_profit(flights):
    view, save = flight_hold(48_000, "460.00")  # selling Rs 480, the airline now Rs 460 (was 457.70)
    kwargs = save.call_args.kwargs
    assert kwargs["selling_total_minor"] == 48_000 and kwargs["supplier_total_minor"] == 46_000
    assert [(t.selling_minor, t.cost_minor) for t in kwargs["travelers"]] == [(48_000, 46_000)]
    assert view.selling_total.amount_minor == 48_000 and view.profit.amount_minor == 2_000 and view.supplier_cost.amount_minor == 46_000


def test_flight_profit_is_hidden_without_cost_access(flights):
    view, _ = flight_hold(48_000, "460.00", viewer=BackOfficeUser("u2", "staff", NONE))
    assert view.profit is None and view.supplier_cost is None


def test_flight_selling_below_the_new_cost_is_blocked(flights):
    with pytest.raises(flight_holds.HoldError) as raised:
        flight_hold(46_700, "470.00")
    assert raised.value.code == "price_changed"


# ---------- Flights: airline price change at ticketing ----------

def ticketing(new_total, accepted=None, viewer=None):
    target = MagicMock(test_booking=False, id="b-1", reference="TRV-AB12CD", pnr="63B55C", provider="southair", currency="PKR", total_minor=48_000, supplier_cost_minor=45_770,
                       supplier_ref="REF", issuance_status="requested", traveler_types=[(0, "ADT")])
    client = MagicMock()
    client.pnr_info = AsyncMock(return_value=pnr_info(total=new_total, tickets=(None,)))
    client.profile = AsyncMock(return_value={"limit": {"cap": "100000.00"}})
    client.ticketing = AsyncMock(return_value={"booking_status": "OK"})
    with patch.object(settings, "southair_ticketing_enabled", True), patch.object(issuance_store, "claim", return_value=target), patch.object(issuance_store, "load", return_value=target), \
         patch.object(issuance_store, "release") as release, patch.object(issuance_store, "update_cost", return_value=target) as update, \
         patch.object(issuance_store, "mark_issued"), patch.object(issuance, "match_tickets", return_value=None):
        try:
            outcome = asyncio.run(issuance.issue("TRV-AB12CD", viewer or SUPER_ADMIN, client_factory=lambda: client, accepted_cost_minor=accepted))
        except issuance_store.IssuanceRefused as error:
            return error, client, update
    return outcome, client, update


def test_ticketing_cost_change_asks_to_confirm_then_issues_at_the_new_cost():
    refused, client, update = ticketing(460.00)
    assert refused.code == "supplier_cost_changed" and refused.extra["accepted_supplier_cost"]["amount_minor"] == 46_000
    assert refused.extra["profit"]["amount_minor"] == 2_000 and client.ticketing.await_count == 0 and update.call_count == 0
    _, client, update = ticketing(460.00, accepted=46_000)
    assert update.call_args.args[2] == 46_000 and client.ticketing.await_count == 1


def test_ticketing_cost_change_hides_cost_without_access():
    refused, _, _ = ticketing(460.00, viewer=BackOfficeUser("u5", "staff", NONE, frozenset({"issue_tickets"})))
    assert refused.code == "supplier_cost_changed" and "profit" not in refused.extra and "new_supplier_cost" not in refused.extra


def test_ticketing_blocked_when_the_airline_price_is_above_the_selling_price():
    refused, client, _ = ticketing(490.00, accepted=49_000)
    assert refused.code == "below_cost" and client.ticketing.await_count == 0


def test_scale_keeps_the_passenger_type_proportions_and_sums_exactly():
    from app.services.pricing import scale
    assert scale([45_770, 45_770, 5_000], 100_001) == [47_411, 47_411, 5_179] and sum(scale([45_770, 45_770, 5_000], 100_001)) == 100_001
    assert scale([None, None], 101) == [51, 50] and scale([], 5) == []
    assert scale([1, 1, 1], 100) == [34, 33, 33]


def test_cost_unknown_for_missing_or_imported_cost_equal_to_total():
    from app.services.pricing import cost_unknown
    assert cost_unknown(None, None, 100) and cost_unknown("legacy_test_csv", 100, 100)
    assert not cost_unknown("legacy_test_csv", 90, 100) and not cost_unknown(None, 100, 100)
