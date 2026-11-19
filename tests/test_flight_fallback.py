"""The paid-but-not-ticketed fallback: reading whether a hold is still there (read-only), re-holding only the same fare at
the same price, and how red alerts reach the admin. Mocked suppliers; no database."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.field_visibility import ALL, NONE, BackOfficeUser, require_back_office
from app.db import issuance as store
from app.main import app
from app.services import flight_fallback, notifications
from app.vendors.flights.southair.client import SouthairError
from tests.southair.fixtures import pnr_info

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def target(provider="southair", expires=NOW + timedelta(hours=2)):
    return store.Target(id="b1", reference="TRV-F-1", pnr="63B55C", provider=provider, product="flight", booking_status="held",
                        issuance_status="not_issued", hold_expires_at=expires, currency="PKR", total_minor=46_700,
                        supplier_cost_minor=45_770, supplier_ref="uuid-1", paid_minor=46_700, traveler_types=[(0, "ADT")])


def southair(answer):
    client = SimpleNamespace(pnr_info=AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(return_value=answer))
    return lambda: client


def sooperfare(listing):
    client = SimpleNamespace(booking_list=AsyncMock(side_effect=listing) if isinstance(listing, Exception) else AsyncMock(return_value=listing))
    return lambda: client


@pytest.mark.parametrize("answer, expected", [
    (pnr_info(), "active"),
    (pnr_info(status="TK", tickets=("2147483647001", "2147483647002"), paid=457.7), "issued"),
    (pnr_info(status="XX"), "unclear"),  # a status we can't read is never treated as lost (a re-hold could double-book)
    (SouthairError("timeout", "x"), "unclear"),
])
def test_southair_hold_status(answer, expected):
    status, words = asyncio.run(flight_fallback.hold_status(target(), NOW, client_factory=southair(answer)))
    assert status == expected and words


@pytest.mark.parametrize("listing, expected", [
    ({"data": [{"uuid": "uuid-1", "booking": {"reservation_status": "active"}}]}, "active"),
    ({"data": [{"uuid": "uuid-1", "booking": {"reservation_status": "expired"}}]}, "lost"),
    ({"data": [{"uuid": "other", "booking": {"reservation_status": "active"}}]}, "unclear"),
    (RuntimeError("down"), "unclear"),
])
def test_sooperfare_hold_status(listing, expected):
    status, _ = asyncio.run(flight_fallback.hold_status(target("sooperfare"), NOW, sooperfare_factory=sooperfare(listing)))
    assert status == expected


def test_a_hold_past_its_time_is_lost_without_asking_the_supplier():
    ask = southair(pnr_info())
    status, _ = asyncio.run(flight_fallback.hold_status(target(expires=NOW - timedelta(minutes=1)), NOW, client_factory=ask))
    assert status == "lost" and ask().pnr_info.await_count == 0


def _rehold(offered, price=46_700):
    """rehold() against a search that offers `offered` segments, repriced at `price`; the hold itself is mocked."""
    from app.db import booking_people
    from app.services import flight_holds, live_flights

    saved = {"direction": "outbound", "origin": "KHI", "destination": "LHE", "flight_number": "PK303",
             "departure_local": "2026-12-10T09:00", "fare_name": "Economy Saver"}
    row = {"provider_id": "southair", "total_minor": 46_700, "currency": "PKR", "itinerary": [saved],
           "supplier_response": {"search": {"origin": "KHI", "destination": "LHE", "departure_date": "2026-12-10", "adults": 1}}}
    people = booking_people.People(
        customer=booking_people.Customer("Test", "Customer", "92", "3001234567", "ops@example.invalid"),
        travelers=[booking_people.Traveler(0, "ADT", "MR", "Test", "Passenger", "PSPT", "PAK", "PAK", datetime(2030, 1, 1).date(), None,
                                           datetime(1990, 1, 1).date(), "AB1234567")], events=[])
    session = SimpleNamespace(fares={"f1": SimpleNamespace(provider="southair", segment={**saved, **offered})})
    hold = AsyncMock(return_value=SimpleNamespace(pnr="NEWPNR", time_limit=NOW + timedelta(hours=3)))
    with patch.object(flight_fallback, "_booking_row", lambda reference: row), patch.object(booking_people, "read", lambda reference: people), \
         patch.object(live_flights, "search", AsyncMock(return_value=SimpleNamespace(search_id="s1"))), \
         patch.object(live_flights, "get_session", lambda search_id, user: session), \
         patch.object(live_flights, "reprice", AsyncMock(return_value=SimpleNamespace(
             quote_id="quote-0001", selling_total=SimpleNamespace(amount_minor=price, currency="PKR")))), \
         patch.object(flight_holds, "hold", hold):
        result = asyncio.run(flight_fallback.rehold("TRV-F-1"))
    return result, hold


def test_rehold_holds_the_same_fare_at_the_same_price_into_the_same_booking():
    (held, what), hold = _rehold({})
    assert held and "NEWPNR" in what
    assert hold.await_args.kwargs["into"] == "TRV-F-1"
    request = hold.await_args.args[0]
    assert request.confirmed_total.amount_minor == 46_700 and request.passengers[0].document_number == "AB1234567"


@pytest.mark.parametrize("offered, price, reason", [
    ({"flight_number": "PK305"}, 46_700, "aren't offered"),  # another flight
    ({"fare_name": "Economy Flex"}, 46_700, "aren't offered"),  # another fare
    ({}, 52_000, "now costs"),  # the same fare at a different price: never re-held
])
def test_rehold_refuses_anything_but_the_same_fare_at_the_paid_price(offered, price, reason):
    (held, what), hold = _rehold(offered, price)
    assert not held and reason in what
    hold.assert_not_awaited()


def test_admin_alerts_are_urgent_and_supplier_names_only_for_allowed_viewers():
    row = {"id": "n1", "kind": "paid_not_issued", "reference": "TRV-F-1", "title": "URGENT: Payment received, ticket NOT issued — TRV-F-1",
           "details": {"supplier": "Southair", "pnr": "63B55C", "urgent": True}, "emailed": False, "created_at": NOW}
    plain = {**row, "id": "n2", "kind": "hold_created", "title": "Seats held: TRV-F-2", "details": {"pnr": "ABC"}}
    client = TestClient(app)
    for visibility, supplier in ((ALL, "Southair"), (NONE, None)):
        app.dependency_overrides[require_back_office] = lambda v=visibility: BackOfficeUser("u1", "staff", v)
        try:
            with patch.object(notifications, "recent", lambda limit: [row, plain]):
                items = client.get("/v1/admin/notifications").json()["items"]
        finally:
            app.dependency_overrides.clear()
        assert [item["urgent"] for item in items] == [True, False]
        assert items[0]["details"].get("supplier") == supplier
