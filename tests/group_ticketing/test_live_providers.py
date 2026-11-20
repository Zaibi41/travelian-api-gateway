"""Live group providers, mocked: listing mapping per provider (Skypass, Dhillu & Sahi), the common
listing fields, provider and supplier PNR only with supplier access, Hold seats only where the
provider holds, and the Skypass hold / release calls (form fields, no expiry in the answer).
Shapes follow the real responses read on 14 Nov 2026 (made-up values)."""

import asyncio
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.field_visibility import ALL, NONE, FieldVisibility
from app.schemas.live_flights import HoldContact, HoldPassenger
from app.services import group_holds
from app.services.group_ticketing_view import baggage_text, ticket_view
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups
from app.vendors.group_ticketing.skypass.holds import SkypassHoldProvider, SkypassRefused
from app.vendors.group_ticketing.skypass.inventory import SkypassTicket, _normalize_ticket, split_baggage

SKYPASS_ROW = {
    "id": 12116, "airline_id": 10, "name": "Fly Jinnah", "image": "1702383592.png", "service_type": "Two Way", "on_hold_rule": 2,
    "meal": "No", "airline_pnr": "4D1HMQ-4RPD3R", "open_seats_slot": 30345, "seats_policy": 0, "adult_price": "160000",
    "child_price": "Price on call", "infants_price": "Price on call", "min_departure_date": "2026-11-16",
    "sectors": [
        {"flight_code": "9P", "flight_num": "700", "departure": "ISB", "destination": "JED", "baggage": "20+07", "meal": "No",
         "sector_type": "Out", "departure_date": "2026-11-16", "departure_time": "06:40", "arrival_time": "10:05:00", "arrival_date": "2026-11-16"},
        {"flight_code": "9P", "flight_num": "701", "departure": "JED", "destination": "ISB", "baggage": "30+10", "meal": "No",
         "sector_type": "In", "departure_date": "2026-12-06", "departure_time": "11:05", "arrival_time": "18:05:00", "arrival_date": "2026-12-06"},
    ],
}
DHILLU_ROWS = [{
    "sector": "LHE - DMM", "airline": {"name": "PF", "logo": "https://api.supplier.invalid/uploads/airlines/1.png"},
    "tickets": [{
        "pnr": "GKTRP5", "date": "2026-11-15", "sector": "LHE - DMM", "air_line": "PF", "meal": 1, "price": "80000",
        "updated_at": "2026-11-14T07:41:48.000000Z", "ticket_type": "1", "baggage": "20", "departure_handcarry": "7", "remaining": 1,
        "subpart": [{"departure_flight_number": "PF742", "departure_sector_name": "LHE - DMM", "departure_baggage": "20", "departure_handcarry": "7",
                     "departure_date": "2026-11-15", "departure_time": "21:10", "arivval_date": "2026-11-15", "arivval_time": "23:45"}],
    }],
}]
STAFF_NAMES = FieldVisibility(supplier_names=True, cost_and_margin=False)


@pytest.fixture(autouse=True)
def markup_and_skypass():
    with patch.object(settings, "group_ticketing_markup_percent", Decimal("5")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0), \
         patch.object(settings, "skypass_endpoint", "https://skypass.invalid"), \
         patch.object(settings, "skypass_client_id", SecretStr("id")), patch.object(settings, "skypass_client_secret", SecretStr("secret")):
        yield


def skypass_ticket():
    return _normalize_ticket(SkypassTicket.model_validate(SKYPASS_ROW)).tickets[0]


@pytest.mark.parametrize("raw, expected", [("20+07", ("20", "7")), ("25+07 kg", ("25", "7")), ("0+7", ("0", "7")),
                                           ("20", ("20", None)), ("", (None, None)), (None, (None, None))])
def test_skypass_baggage_string_splits_into_checked_and_hand_carry(raw, expected):
    assert split_baggage(raw) == expected


def test_skypass_mapping_both_legs_flight_numbers_price_per_adult_and_hold_ids():
    ticket = skypass_ticket()
    assert (ticket.vendor, ticket.pnr, ticket.sector, ticket.airline, ticket.ticket_type) == ("skypass", "4D1HMQ-4RPD3R", "ISB - JED", "Fly Jinnah", "Two Way")
    assert ticket.price == "160000"  # adult_price: per adult; child and infant prices are separate fields
    assert (ticket.baggage, ticket.hand_carry) == ("20", "7")
    assert (ticket.source_ticket_id, ticket.source_slot) == ("12116", "30345")
    outbound, back = ticket.legs
    assert (outbound.direction, outbound.flight_number, outbound.departure_time, outbound.arrival_time) == ("outbound", "9P700", "06:40", "10:05")
    assert (back.direction, back.flight_number, back.origin, back.destination, back.baggage, back.hand_carry) == ("return", "9P701", "JED", "ISB", "30", "10")


def test_dhillu_mapping_hand_carry_flight_number_and_one_leg():
    ticket = normalize_groups(DHILLU_ROWS)[0].tickets[0]
    assert (ticket.vendor, ticket.pnr, ticket.price, ticket.remaining) == ("dhillu_sahi", "GKTRP5", "80000", 1)
    assert (ticket.baggage, ticket.hand_carry, ticket.meal, ticket.ticket_type) == ("20", "7", 1, "1")
    [leg] = ticket.legs
    assert (leg.flight_number, leg.origin, leg.destination, leg.departure_time, leg.arrival_time, leg.hand_carry) == ("PF742", "LHE", "DMM", "21:10", "23:45", "7")


def test_listing_view_common_fields_price_from_listing_and_meal_text():
    view = ticket_view(skypass_ticket(), NONE)
    assert view.selling_price.amount_minor == 168_000_00 and view.selling_price.currency == "PKR"  # 160,000 + 5%, per adult
    assert (view.flight_number, view.departure_time, view.meal, view.baggage, view.hand_carry) == ("9P700", "06:40", "No", "20", "7")
    assert [leg.direction for leg in view.legs] == ["outbound", "return"] and view.legs[1].baggage == "30"
    dhillu = ticket_view(normalize_groups(DHILLU_ROWS)[0].tickets[0], NONE)
    assert (dhillu.meal, dhillu.flight_number, dhillu.hand_carry) == ("Yes", "PF742", "7")
    assert dhillu.selling_price.amount_minor == 84_000_00


@pytest.mark.parametrize("visibility, sees", [(NONE, False), (FieldVisibility(False, True), False), (STAFF_NAMES, True), (ALL, True)])
def test_provider_and_supplier_pnr_only_with_supplier_access(visibility, sees):
    for ticket in (skypass_ticket(), normalize_groups(DHILLU_ROWS)[0].tickets[0]):
        body = ticket_view(ticket, visibility).model_dump(exclude_none=True)
        assert ("supplier" in body and "pnr" in body) is sees
        if not sees:
            text = str(body)
            assert ticket.vendor not in text and ticket.pnr not in text
        assert body["stock"] == "supplier"
    # The hold ids a provider needs never leave the gateway.
    assert "30345" not in str(ticket_view(skypass_ticket(), ALL).model_dump())


@pytest.mark.parametrize("checked, hand, expected", [
    ("30", "10", "10+30 kg"), ("20", "7", "7+20 kg"), ("20 KG", "07", "7+20 kg"), ("0", "7", "7 kg"),
    # No checked value at all: not read as "no checked bag" (#36).
    ("", "7", "7 kg hand carry"), (None, 7, "7 kg hand carry"), ("20", None, "20 kg checked"),
    ("", "", None), (None, None, None), ("see supplier", None, None),
])
def test_group_baggage_text_puts_hand_carry_first(checked, hand, expected):
    assert baggage_text(checked, hand) == expected


def test_hold_only_where_the_provider_holds_and_a_selling_price_exists():
    skypass = ticket_view(skypass_ticket(), NONE)
    assert skypass.can_hold is True and skypass.hold_unavailable_reason is None
    with patch.object(settings, "group_dhillu_sahi_hold_enabled", False):  # the off switch: today's reasons
        dhillu = ticket_view(normalize_groups(DHILLU_ROWS)[0].tickets[0], NONE)
        assert dhillu.can_hold is False and dhillu.hold_unavailable_reason == "Holding isn't available for this provider"
        named = ticket_view(normalize_groups(DHILLU_ROWS)[0].tickets[0], ALL)
        assert named.can_hold is False and named.hold_unavailable_reason == "Dhillu & Sahi has no hold API"
    with patch.object(settings, "group_dhillu_sahi_hold_enabled", True), patch.object(settings, "dhillu_sahi_email", "a@example.invalid"), \
         patch.object(settings, "dhillu_sahi_password", SecretStr("synthetic")):
        assert ticket_view(normalize_groups(DHILLU_ROWS)[0].tickets[0], NONE).can_hold is True
    with patch.object(settings, "skypass_endpoint", None):  # Skypass not configured: no holds either
        assert ticket_view(skypass_ticket(), NONE).can_hold is False
    with patch.object(settings, "group_ticketing_markup_percent", None), patch.object(settings, "group_ticketing_markup_fixed_minor", None):
        unpriced = ticket_view(skypass_ticket(), NONE)
    # No markup set: markup 0, the listing price per adult, and Hold seats stays on.
    assert unpriced.can_hold is True and unpriced.hold_unavailable_reason is None
    assert unpriced.selling_price is not None and unpriced.selling_price.amount_minor == 160_000_00
    no_price = skypass_ticket().model_copy(update={"price": "Price on call"})
    assert ticket_view(no_price, NONE).hold_unavailable_reason == "The provider sent no price for this fare"


class Recorder:
    def __init__(self, hold_answer=None, release_answer=None):
        self.requests: list[tuple[str, dict]] = []
        self.hold_answer = hold_answer or {"status": "success", "bal_seats": "2", "message": "2 Ticket(s) on hold"}
        self.release_answer = release_answer or {"status": "success", "message": "Ticket on hold free now."}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fields = {}
        if request.method == "POST" and request.content:
            for part in request.content.split(b"--")[1:]:
                if b'name="' in part:
                    name = part.split(b'name="')[1].split(b'"')[0].decode()
                    value = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0].decode() if b"\r\n\r\n" in part else ""
                    fields[name] = value
        self.requests.append((path, fields))
        if path == "/auth":
            return httpx.Response(200, json={"access_token": "token"})
        if path == "/hold-available-seats":
            return httpx.Response(200, json=self.hold_answer)
        if path == "/release-seats":
            return httpx.Response(200, json=self.release_answer)
        if path.startswith("/ticket/"):
            return httpx.Response(200, json={"tickets": {"open_adult_price": "162000", "airline_pnr": "4D1HMQ-4RPD3R"}})
        return httpx.Response(404, json={})


ADULT = HoldPassenger(type="ADT", title="MR", first_name="Ali", last_name="Raza", date_of_birth="1988-03-14", id_type="PSPT",
                      document_number="AB1234567", document_country="PAK", document_expiry="2031-09-30", nationality="PAK")
CONTACT = HoldContact(first_name="Ali", last_name="Raza", email="a@example.invalid", phone_code="92", phone="3001234567")


def test_skypass_hold_sends_ticket_quantity_and_slot_and_gives_no_expiry():
    recorder = Recorder()
    provider = SkypassHoldProvider(transport=httpx.MockTransport(recorder))
    held = asyncio.run(provider.hold(skypass_ticket(), [ADULT, ADULT], CONTACT))
    assert recorder.requests[1] == ("/hold-available-seats", {"ticket_id": "12116", "quantity": "2", "open_seats_slot": "30345"})
    assert held.supplier_ref == "12116:30345" and held.pnr == "4D1HMQ-4RPD3R"
    assert held.cost_per_adult.amount_minor == 160_000_00  # from the listing: the hold answer has no price
    assert not hasattr(held, "expires_at")
    asyncio.run(provider.release(group_holds.HeldRef(held.pnr, held.supplier_ref, 2)))
    assert recorder.requests[-1] == ("/release-seats", {"ticket_id": "12116", "open_seats_slot": "30345"})
    assert asyncio.run(provider.price(group_holds.HeldRef(held.pnr, held.supplier_ref, 2))).amount_minor == 2 * 162_000_00  # both seats
    assert provider.supports_issue is False


def test_skypass_refusals_are_clear():
    refused = SkypassHoldProvider(transport=httpx.MockTransport(Recorder(hold_answer={"status": "error", "message": "No seats"})))
    with pytest.raises(SkypassRefused):
        asyncio.run(refused.hold(skypass_ticket(), [ADULT], CONTACT))
    not_released = SkypassHoldProvider(transport=httpx.MockTransport(Recorder(release_answer={"status": "error", "message": "x"})))
    with pytest.raises(SkypassRefused):
        asyncio.run(not_released.release(group_holds.HeldRef("P", "12116:30345", 1)))
