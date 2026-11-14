"""Group identity on every fare: provider, supplier PNR, supplier group ID and supplier ticket ID are kept on
each listing row and copied onto our booking when a hold is created. Only viewers with supplier access
(Super Admin; staff with "see supplier names") receive them; everyone else gets "supplier"/"owned" only.

Field mapping (raw supplier fields, read from the live listing on 14 Nov 2026):
- Dhillu & Sahi: ticket `pnr`; first `subpart[].group_id` (group); first `subpart[].id` (ticket).
- Skypass: `airline_pnr`; no group ID (Skypass sends none); ticket `id`. `open_seats_slot` is hold-only and never sent.
Mocked data only; no supplier is called."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes import group_ticketing as group_routes
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, require_search_viewer, resolve_actions
from app.db.bookings import BookingRow
from app.db.group_holds import GroupBooking
from app.services import admin_bookings
from app.services import group_holds
from app.services.group_ticketing_view import inventory_view, ticket_view
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups
from app.vendors.group_ticketing.skypass.inventory import SkypassTicket, _normalize_ticket
from tests.group_ticketing.test_live_providers import SKYPASS_ROW

STAFF_NAMES = FieldVisibility(supplier_names=True, cost_and_margin=False)
STAFF_COST = FieldVisibility(supplier_names=False, cost_and_margin=True)
SUPER_ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))
STAFF = BackOfficeUser("u-2", "staff", NONE)
IDENTITY_KEYS = {"group_source", "supplier", "pnr", "supplier_pnr", "supplier_group_id", "supplier_ticket_id", "provider_name"}

DHILLU_ROWS = [{
    "sector": "LHE - DMM", "airline": {"name": "PF", "logo": None},
    "tickets": [{
        "pnr": "41SDS4", "date": "2026-11-20", "sector": "LHE - DMM", "air_line": "PF", "meal": 1, "price": "80000",
        "updated_at": "2026-11-14T07:41:48.000000Z", "ticket_type": "1", "baggage": "20", "departure_handcarry": "7", "remaining": 4,
        "subpart": [
            {"id": 10817, "group_id": 3056, "departure_flight_number": "PF742", "departure_sector_name": "LHE - DMM", "departure_date": "2026-11-20",
             "departure_time": "21:10", "arivval_date": "2026-11-20", "arivval_time": "23:45"},
            {"id": 10818, "group_id": 3056, "departure_flight_number": "PF743", "departure_sector_name": "DMM - RUH"},
        ],
    }],
}]


@pytest.fixture(autouse=True)
def markup():
    with patch.object(settings, "group_ticketing_markup_percent", Decimal("0")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0):
        yield


def dhillu():
    return normalize_groups(DHILLU_ROWS)[0].tickets[0]


def skypass():
    return _normalize_ticket(SkypassTicket.model_validate(SKYPASS_ROW)).tickets[0]


def all_keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in all_keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in all_keys(v)}
    return set()


# ---------- mapping ----------

def test_dhillu_identity_is_the_first_subpart_group_id_and_id():
    ticket = dhillu()
    assert (ticket.vendor, ticket.pnr, ticket.supplier_group_id, ticket.supplier_ticket_id) == ("dhillu_sahi", "41SDS4", "3056", "10817")


def test_dhillu_without_subparts_has_no_ids():
    rows = [{**DHILLU_ROWS[0], "tickets": [{**DHILLU_ROWS[0]["tickets"][0], "subpart": []}]}]
    ticket = normalize_groups(rows)[0].tickets[0]
    assert (ticket.supplier_group_id, ticket.supplier_ticket_id) == (None, None)


def test_skypass_identity_is_the_ticket_id_and_no_group():
    ticket = skypass()
    assert (ticket.vendor, ticket.pnr, ticket.supplier_group_id, ticket.supplier_ticket_id) == ("skypass", "4D1HMQ-4RPD3R", None, "12116")
    assert "30345" not in ticket_view(ticket, ALL).model_dump_json()  # open_seats_slot is hold-only


# ---------- listing ----------

@pytest.mark.parametrize("visibility", [ALL, STAFF_NAMES])
def test_listing_row_carries_the_source_with_supplier_access(visibility):
    source = ticket_view(dhillu(), visibility).group_source
    assert source.model_dump() == {"provider": "dhillu_sahi", "provider_name": "Dhillu & Sahi", "supplier_pnr": "41SDS4",
                                   "supplier_group_id": "3056", "supplier_ticket_id": "10817"}
    sky = ticket_view(skypass(), visibility).group_source
    assert (sky.provider_name, sky.supplier_pnr, sky.supplier_group_id, sky.supplier_ticket_id) == ("Skypass", "4D1HMQ-4RPD3R", None, "12116")


@pytest.mark.parametrize("visibility", [NONE, STAFF_COST])
def test_listing_row_has_no_source_without_supplier_access(visibility):
    for ticket in (dhillu(), skypass()):
        body = ticket_view(ticket, visibility).model_dump(exclude_none=True)
        assert not (all_keys(body) & IDENTITY_KEYS)
        text = str(body)
        for value in (ticket.pnr, ticket.supplier_group_id, ticket.supplier_ticket_id, "Dhillu", "Skypass", ticket.vendor):
            assert value is None or value not in text
        assert body["stock"] == "supplier"


def test_search_route_sends_the_source_only_to_viewers_with_supplier_access(monkeypatch):
    async def fetch():
        return normalize_groups(DHILLU_ROWS)

    monkeypatch.setattr(group_routes, "fetch_all_group_inventory", fetch)
    app = FastAPI()
    app.include_router(group_routes.router, prefix="/v1")
    for viewer, sees in ((SUPER_ADMIN, True), (STAFF, False)):
        app.dependency_overrides[require_search_viewer] = lambda viewer=viewer: viewer
        response = TestClient(app).get("/v1/group-ticketing/inventory")
        assert response.status_code == 200, response.text
        body = response.json()
        assert ("group_source" in all_keys(body)) is sees
        assert ("3056" in response.text and "10817" in response.text and "41SDS4" in response.text) is sees


def test_inventory_view_keeps_the_stock_label_for_everyone():
    for visibility in (NONE, ALL):
        [group] = inventory_view(normalize_groups(DHILLU_ROWS), visibility).data
        assert group.tickets[0].stock == "supplier"


# ---------- the hold record ----------

def test_hold_record_keeps_the_listing_identity_and_the_held_pnr():
    assert group_holds.source_record(dhillu(), None) == {"provider": "dhillu_sahi", "supplier_pnr": "41SDS4",
                                                          "supplier_group_id": "3056", "supplier_ticket_id": "10817"}
    # Skypass answers the hold with its own PNR; that one is kept.
    assert group_holds.source_record(skypass(), "4FGYK2-4B4VYB")["supplier_pnr"] == "4FGYK2-4B4VYB"


def booking(source):
    now = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
    return GroupBooking("b-1", "TRV-GRP-AB12CD", "41SDS4", "dhillu_sahi", "held", "not_issued", "unpaid", now + timedelta(minutes=30),
                        "PKR", 160_000_00, 160_000_00, "SUPPLIER-REF", [], 2, 0, {}, source), now


@pytest.mark.parametrize("viewer, sees", [
    (SUPER_ADMIN, True), (BackOfficeUser("u-3", "staff", STAFF_NAMES), True), (STAFF, False),
    (BackOfficeUser("u-4", "staff", STAFF_COST), False),
])
def test_held_and_booking_page_view_has_the_source_only_with_supplier_access(viewer, sees):
    record, now = booking({"provider": "dhillu_sahi", "supplier_pnr": "41SDS4", "supplier_group_id": "3056", "supplier_ticket_id": "10817"})
    with patch.object(group_holds, "provider_for", lambda vendor: None):
        view = group_holds.build_view(record, None, viewer, now)
    body = view.model_dump_json(exclude_none=True)
    if sees:
        assert view.group_source.model_dump() == {"provider": "dhillu_sahi", "provider_name": "Dhillu & Sahi", "supplier_pnr": "41SDS4",
                                                   "supplier_group_id": "3056", "supplier_ticket_id": "10817"}
    else:
        assert view.group_source is None and view.supplier is None and view.pnr is None
        for value in ("41SDS4", "3056", "10817", "dhillu", "Dhillu"):
            assert value not in body
    assert view.stock == "supplier"


def test_hold_made_before_the_identity_was_kept_falls_back_to_provider_and_pnr():
    record, now = booking({})
    with patch.object(group_holds, "provider_for", lambda vendor: None):
        source = group_holds.build_view(record, None, SUPER_ADMIN, now).group_source
    assert (source.provider_name, source.supplier_pnr, source.supplier_group_id, source.supplier_ticket_id) == ("Dhillu & Sahi", "41SDS4", None, None)


# ---------- Bookings list and detail: a group booking's PNR is the supplier's ----------

def row(product):
    return BookingRow(booking_id=f"TRV-{product}", pnr="41SDS4" if product == "group" else "ABC123", product=product, provider="dhillu_sahi",
                      status="held", payment_status="unpaid", ticket_status="not_issued", created_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
                      hold_expires_at=None, currency="PKR", total_minor=1, supplier_cost_minor=1)


def test_group_booking_pnr_only_with_supplier_access_in_bookings_list_and_detail():
    rows = [row("group"), row("flight")]
    now = datetime(2026, 11, 14, 1, tzinfo=timezone.utc)
    for visibility, sees in ((ALL, True), (STAFF_NAMES, True), (NONE, False), (STAFF_COST, False)):
        listed = admin_bookings.build_list(rows, admin_bookings.BookingListQuery(), now, 24, visibility)
        pnrs = {item.booking_id: item.pnr for item in listed.items}
        assert pnrs["TRV-flight"] == "ABC123"
        assert (pnrs["TRV-group"] == "41SDS4") is sees and (pnrs["TRV-group"] is None) is not sees
        assert (admin_bookings.build_detail(rows[0], visibility).pnr == "41SDS4") is sees
        # Searching by the supplier PNR finds nothing without access.
        found = admin_bookings.build_list(rows, admin_bookings.BookingListQuery(q="41sds4"), now, 24, visibility)
        assert (found.total == 1) is sees
