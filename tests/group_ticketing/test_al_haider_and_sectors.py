"""Al Haider listing (permanent Bearer token, no login) and Dhillu & Sahi listing per sector group id from
DHILLU_SAHI_SECTOR_GROUP_IDS (no default), merged. Mocked HTTP only; shapes follow the real responses read on 14 Nov 2026 (made-up values)."""

import asyncio
import json
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.field_visibility import ALL, NONE, FieldVisibility
from app.services.group_ticketing_view import baggage_text, inventory_view, ticket_view
from app.vendors.group_ticketing.al_haider import inventory as al_haider
from app.vendors.group_ticketing.dhillu_sahi import inventory as dhillu
from app.vendors.group_ticketing.dhillu_sahi.auth import DhilluSahiAuthClient

TOKEN = "synthetic-permanent-token-0123456789"
STAFF_NAMES = FieldVisibility(supplier_names=True, cost_and_margin=False)


def al_group(id_=3437, sector="ISB-DMM", details=None, price=98000, pax=4, meal="No"):
    return {
        "id": id_, "airline_id": 5, "available_no_of_pax": pax, "dept_date": "2026-11-21", "arv_date": None, "sector": sector,
        "type": "KSA ONEWAY", "price": price, "baggage": None, "meal": meal, "pnr": "4UD1CN-49Z0AJ", "effective_dept_date": "2026-11-21",
        "details": details or [{"id": 71284, "group_id": id_, "sr": 1, "flight_no": "9P-710", "flight_date": "2026-11-21", "origin": "Islamabad",
                                "destination": "Dammam", "baggage": "20+7", "dept_time": "19:55:00", "arv_time": "22:15:00"}],
        "airline": {"id": 5, "airline_name": "FLY JINNAH", "short_name": "9P", "path_type": "upload", "logo_path": "airlines/1.png",
                    "logo_url": "https://supplier.invalid/storage/airlines/1.png"},
    }


CONNECTING = al_group(3486, "ISB-MCT-JED", meal="Yes", details=[
    {"id": 71511, "group_id": 3486, "sr": 2, "flight_no": "OV-283", "flight_date": "2026-11-18", "origin": "Muscat", "destination": "Jeddah",
     "baggage": "0+7", "dept_time": "08:40:00", "arv_time": "11:10:00"},
    {"id": 71510, "group_id": 3486, "sr": 1, "flight_no": "OV-564", "flight_date": "2026-11-18", "origin": "Islamabad", "destination": "Muscat",
     "baggage": "0+7", "dept_time": "04:25:00", "arv_time": "06:40:00"},
])


@pytest.fixture(autouse=True)
def configured():
    with patch.object(settings, "al_haider_base_url", "https://al-haider.invalid"), patch.object(settings, "al_haider_access_token", SecretStr(TOKEN)), \
         patch.object(settings, "group_ticketing_markup_percent", Decimal("0")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0):
        yield


# ---------- Al Haider ----------

def test_al_haider_maps_into_the_common_listing():
    [group] = al_haider.normalize([al_group()])
    ticket = group.tickets[0]
    assert (ticket.vendor, ticket.pnr, ticket.supplier_group_id, ticket.supplier_ticket_id) == ("al_haider", "4UD1CN-49Z0AJ", "3437", "71284")
    view = ticket_view(ticket, ALL)
    assert (view.sector, view.departure_date, view.departure_time, view.flight_number, view.airline) == ("ISB - DMM", "2026-11-21", "19:55", "9P710", "FLY JINNAH")
    assert view.remaining == 4 and view.meal == "No" and view.ticket_type == "KSA ONEWAY"
    assert baggage_text(view.baggage, view.hand_carry) == "7+20 kg"  # "20+7": 20 kg checked, 7 kg hand carry; hand carry first
    assert view.selling_price.model_dump() == {"amount_minor": 98_000_00, "currency": "PKR"}  # per passenger, Rs
    assert view.logo_url.startswith("/v1/logos/") and "supplier.invalid" not in view.model_dump_json()


def test_al_haider_connecting_flights_take_codes_from_the_sector_in_order():
    ticket = al_haider.normalize([CONNECTING])[0].tickets[0]
    assert [(leg.flight_number, leg.origin, leg.destination, leg.departure_time) for leg in ticket.legs] == [
        ("OV564", "ISB", "MCT", "04:25"), ("OV283", "MCT", "JED", "08:40")]
    view = ticket_view(ticket, NONE)
    assert view.sector == "ISB - MCT - JED" and view.departure_time == "04:25" and view.meal == "Yes"
    assert baggage_text(view.baggage, view.hand_carry) == "7 kg"  # hand carry only
    assert ticket.supplier_ticket_id == "71510"


def test_al_haider_hold_is_off_with_its_reason_named_only_with_supplier_access():
    ticket = al_haider.normalize([al_group()])[0].tickets[0]
    for visibility in (ALL, STAFF_NAMES):
        view = ticket_view(ticket, visibility)
        assert view.can_hold is False and view.hold_unavailable_reason == "Holding is not wired for Al Haider yet"
        assert view.group_source.model_dump() == {"provider": "al_haider", "provider_name": "Al Haider", "supplier_pnr": "4UD1CN-49Z0AJ",
                                                   "supplier_group_id": "3437", "supplier_ticket_id": "71284"}
    staff = ticket_view(ticket, NONE).model_dump_json(exclude_none=True)
    assert '"hold_unavailable_reason":"Holding isn\'t available for this provider"' in staff and '"stock":"supplier"' in staff
    for leak in ("Al Haider", "al_haider", "4UD1CN", "3437", "71284", "group_source"):
        assert leak not in staff


def test_al_haider_sends_the_token_as_bearer_with_no_login_and_never_returns_it():
    seen = []

    def supplier(request):
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers.get("authorization")))
        return httpx.Response(200, json={"groups": [al_group(), CONNECTING]})

    groups = asyncio.run(al_haider.fetch_inventory(httpx.MockTransport(supplier)))
    assert seen == [("GET", "/api/available/groups", {"type": "", "airline_id": "", "sector": "", "dept_date": ""}, f"Bearer {TOKEN}")]
    assert len(groups) == 2
    assert TOKEN not in json.dumps(inventory_view(groups, ALL).model_dump())


@pytest.mark.parametrize("answer, code", [
    (httpx.Response(401, json={"message": "Unauthenticated."}), "upstream_http_401"),
    (httpx.Response(200, json={"data": []}), "invalid_response"),
    (httpx.Response(200, json={"groups": [{"sector": "ISB-DMM"}]}), "invalid_response"),
])
def test_al_haider_failures_are_clear_and_carry_no_token(answer, code):
    with pytest.raises(al_haider.AlHaiderError) as raised:
        asyncio.run(al_haider.fetch_inventory(httpx.MockTransport(lambda request: answer)))
    assert raised.value.error_code == code and TOKEN not in str(raised.value)


def test_al_haider_needs_both_url_and_token():
    with patch.object(settings, "al_haider_access_token", None):
        assert not al_haider.is_configured()
    assert al_haider.is_configured()


# ---------- Dhillu & Sahi sector group ids (made-up ids: the tests set their own) ----------

def ds_rows(sector, code, subpart_ids, pnr="PNR1"):
    return [{"sector": sector, "airline": {"name": "PF", "logo": None}, "tickets": [{
        "pnr": f"{pnr}-{sid}", "date": "2026-11-20", "sector": f"LHE - {code}", "price": "85000", "remaining": 5, "updated_at": "2026-11-14T10:00:00Z",
        "ticket_type": "1", "subpart": [{"id": sid, "group_id": 3000 + sid, "departure_flight_number": "PF742", "departure_sector_name": f"LHE - {code}"}],
    } for sid in subpart_ids]}]


@pytest.fixture
def ds_configured():
    with patch.object(settings, "dhillu_sahi_email", "a@example.invalid"), patch.object(settings, "dhillu_sahi_password", SecretStr("synthetic")), \
         patch.object(settings, "dhillu_sahi_sector_group_ids", "101,102,103"):
        yield


def ds_client(answers, sent):
    def supplier(request):
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"success": {"token": "login-token", "agent_id": 41}})
        body = json.loads(request.content)
        sent.append(body)
        answer = answers[body["my_sector_id"]]
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json={"success": True, "data": answer})
    return DhilluSahiAuthClient("https://dhillu.invalid", transport=httpx.MockTransport(supplier))


def test_no_code_default_for_the_sector_group_ids():
    assert type(settings).model_fields["dhillu_sahi_sector_group_ids"].default is None


@pytest.mark.parametrize("value, ids", [("101,102,103", ["101", "102", "103"]), (" 101 , ,102,, 101 ", ["101", "102"]), ("abc-7", ["abc-7"])])
def test_ids_are_read_as_written_trimmed_without_empty_items(value, ids):
    with patch.object(settings, "dhillu_sahi_sector_group_ids", value):
        assert dhillu.sector_group_ids() == ids


@pytest.mark.parametrize("value", [None, "", " , ,"])
def test_unset_or_empty_ids_mean_not_configured_and_no_listing_call(ds_configured, value, caplog):
    sent = []
    with patch.object(settings, "dhillu_sahi_sector_group_ids", value):
        assert dhillu.sector_group_ids() == [] and not dhillu.is_configured()
        from app.services.group_ticketing import get_group_inventory_vendors
        assert "dhillu_sahi" not in [name for name, _ in get_group_inventory_vendors()]
        with pytest.raises(RuntimeError):
            asyncio.run(dhillu.fetch_inventory(ds_client({}, sent)))
        assert dhillu.warn_if_ids_missing() is True
    assert sent == []
    [warning] = [r for r in caplog.records if r.levelname == "WARNING"]
    assert "DHILLU_SAHI_SECTOR_GROUP_IDS is not set" in warning.getMessage()


def test_no_startup_warning_when_ids_are_set(ds_configured, caplog):
    assert dhillu.warn_if_ids_missing() is False and not caplog.records


def test_one_listing_call_per_id_merged_and_deduplicated_by_supplier_ticket_id(ds_configured):
    sent = []
    answers = {"101": ds_rows("LAHORE - DAMMAM", "DMM", [1, 2]), "102": ds_rows("LAHORE - KUWAIT", "KWI", [3]) + ds_rows("LAHORE - DAMMAM", "DMM", [2]),
               "103": ds_rows("LAHORE - MUSCAT", "MCT", [4, 3])}
    groups = asyncio.run(dhillu.fetch_inventory(ds_client(answers, sent)))
    assert sorted(body["my_sector_id"] for body in sent) == ["101", "102", "103"]
    tickets = [t for g in groups for t in g.tickets]
    assert sorted(t.supplier_ticket_id for t in tickets) == ["1", "2", "3", "4"]  # tickets 2 and 3 listed twice: kept once
    assert len({g.id for g in groups}) == len(groups)  # row positions from different ids don't collide
    # Every merged route reaches the sector dropdown and suggested routes (both are built from the merged listing).
    view = inventory_view(groups, NONE)
    assert {ticket.sector for group in view.data for ticket in group.tickets} == {"LHE - DMM", "LHE - KWI", "LHE - MCT"}


def test_one_failing_id_still_returns_the_others_and_is_logged(ds_configured, caplog):
    sent = []
    answers = {"101": ds_rows("LAHORE - DAMMAM", "DMM", [1]), "102": httpx.Response(500, json={}), "103": ds_rows("LAHORE - MUSCAT", "MCT", [5])}
    groups = asyncio.run(dhillu.fetch_inventory(ds_client(answers, sent)))
    assert [t.supplier_ticket_id for g in groups for t in g.tickets] == ["1", "5"]
    failure = next(r for r in caplog.records if r.getMessage() == "Dhillu & Sahi listing failed for one sector group")
    assert (failure.sector_group_id, failure.error_code) == ("102", "upstream_http_500")


def test_every_id_failing_fails_the_supplier(ds_configured):
    answers = {"101": httpx.Response(404, json={}), "102": httpx.Response(500, json={}), "103": httpx.Response(500, json={})}
    with pytest.raises(Exception) as raised:
        asyncio.run(dhillu.fetch_inventory(ds_client(answers, [])))
    assert getattr(raised.value, "error_code", "").startswith("upstream_http_")
