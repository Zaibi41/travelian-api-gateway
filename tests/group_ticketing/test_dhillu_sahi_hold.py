"""Dhillu & Sahi hold through add-booking, with the supplier's HTTP mocked (no live call). Shapes: add-booking
{"success": true, "data": {"id": <int>}}; agent-booking-detail {"success": true, "data": {"data": {...booking},
"groupdata": {...group}}, "message": ...}. Payload: pnr and updated_at from the fresh listing, agent, remarks "",
equal-length traveler arrays, type "adult" only. No release call exists: nothing is released at our expiry."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, resolve_actions
from app.db import group_holds as store
from app.schemas.group_holds import GroupHoldRequest
from app.schemas.live_flights import HoldContact, HoldPassenger
from app.services import flight_holds, group_hold_expiry, group_holds
from app.services.group_holds import GroupHoldError
from app.services.group_ticketing import group_ticket_id
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups
from app.vendors.group_ticketing.dhillu_sahi.holds import DhilluSahiHoldProvider, booking_payload, missing_traveler_details

ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))
STAFF = BackOfficeUser("u-2", "staff", NONE)
UPDATED = "2026-11-14T15:09:58.000000Z"


def rows(price="85000", remaining=6, updated_at=UPDATED):
    return [{"sector": "LHE - DMM", "airline": {"name": "PF", "logo": None}, "tickets": [{
        "pnr": "4FMUAF - 4KOCE5", "date": "2026-11-19", "sector": "LHE - DMM", "air_line": "PF", "meal": 1, "price": price,
        "updated_at": updated_at, "ticket_type": "1", "baggage": "20", "departure_handcarry": "7", "remaining": remaining,
        "subpart": [{"id": 10817, "group_id": 3056, "departure_flight_number": "PF742", "departure_sector_name": "LHE - DMM",
                     "departure_date": "2026-11-19", "departure_time": "21:10"}]}]}]


def ticket(**kw):
    return normalize_groups(rows(**kw))[0].tickets[0]


def adult(first="Test", passport="TEST12345", **kw):
    return HoldPassenger.model_validate({"type": "ADT", "title": "MR", "first_name": first, "last_name": "Traveler", "date_of_birth": "1990-01-01",
                                         "id_type": "PSPT", "document_number": passport, "document_country": "PAK",
                                         "document_expiry": "2031-01-01", "nationality": "PAK", **kw})


CONTACT = HoldContact(first_name="Test", last_name="Traveler", email="t@example.invalid", phone_code="92", phone="3001234567")


class Supplier:
    """Answers login, add-booking and agent-booking-detail; records every request (path, JSON body, auth header)."""

    def __init__(self, add_status=200, add_body=None, add_error=None):
        self.requests = []
        self.add_status, self.add_error = add_status, add_error
        self.add_body = add_body if add_body is not None else {"success": True, "data": {"id": 27049}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.url.path, body, request.headers.get("authorization")))
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"success": {"token": "login-token", "agent_id": 41}})
        if request.url.path == "/api/add-booking":
            if self.add_error:
                raise self.add_error
            return httpx.Response(self.add_status, json=self.add_body)
        if request.url.path == "/api/agent-booking-detail":
            return httpx.Response(200, json={"success": True, "message": "Booking Details Fetch Successful", "data": {
                "data": {"id": 27049, "status": "hold", "booking_sub": [{"firstname": "Test", "passport_no": "TEST12345"}]},
                "groupdata": {"id": 3056, "pnr": "4FMUAF - 4KOCE5", "price": "85000", "subpart": []}}})
        return httpx.Response(404, json={})

    def paths(self):
        return [path for path, _, _ in self.requests]


@pytest.fixture(autouse=True)
def configured():
    with patch.object(settings, "dhillu_sahi_email", "a@example.invalid"), patch.object(settings, "dhillu_sahi_password", SecretStr("synthetic")), \
         patch.object(settings, "dhillu_sahi_token", None), patch.object(settings, "dhillu_sahi_agent_id", None), \
         patch.object(settings, "group_dhillu_sahi_hold_enabled", True), \
         patch.object(settings, "group_ticketing_markup_percent", Decimal("0")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0):
        yield


# ---------- payload ----------

def test_payload_uses_the_listing_pnr_and_updated_at_and_aligned_adult_arrays():
    body = booking_payload(ticket(), [adult("Ali", "AB1234567"), adult("Sana", "CD7654321", title="MRS", date_of_birth="1992-05-02")], agent=41)
    assert body == {
        "pnr": "4FMUAF - 4KOCE5", "agent": 41, "updated_at": UPDATED, "remarks": "",
        "titles": ["MR", "MRS"], "firstname": ["Ali", "Sana"], "lastname": ["Traveler", "Traveler"], "dob": ["1990-01-01", "1992-05-02"],
        "passport_no": ["AB1234567", "CD7654321"], "passport_expiry_date": ["2031-01-01", "2031-01-01"], "type": ["adult", "adult"],
    }
    arrays = [body[k] for k in ("titles", "firstname", "lastname", "dob", "passport_no", "passport_expiry_date", "type")]
    assert {len(a) for a in arrays} == {2}


def test_payload_refuses_children_and_missing_listing_values():
    with pytest.raises(ValueError):
        booking_payload(ticket(), [adult(type="CHD")], agent=41)
    with pytest.raises(ValueError):
        booking_payload(ticket().model_copy(update={"updated_at": None}), [adult()], agent=41)


def test_every_traveler_needs_a_passport():
    assert missing_traveler_details([adult(), adult(id_type="NATI", document_number="3520112345671", document_country="PAK")]) == [(1, "document_number")]


# ---------- provider ----------

def run(coro):
    return asyncio.run(coro)


def test_hold_sends_one_add_booking_then_reads_the_detail_and_keeps_both():
    supplier = Supplier()
    held = run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert supplier.paths() == ["/api/login", "/api/add-booking", "/api/agent-booking-detail"]
    _, sent, auth = supplier.requests[1]
    assert sent["pnr"] == "4FMUAF - 4KOCE5" and sent["updated_at"] == UPDATED and sent["agent"] == 41 and auth == "Bearer login-token"
    assert supplier.requests[2][1] == {"booking_id": 27049}
    assert (held.pnr, held.supplier_ref, held.cost_per_adult.amount_minor) == ("4FMUAF - 4KOCE5", "27049", 85_000_00)
    assert held.supplier_answer["booking_id"] == 27049 and held.supplier_answer["add_booking"]["data"]["id"] == 27049
    detail = held.supplier_answer["detail"]
    assert detail["booking"] == {"id": 27049, "status": "hold"}  # traveler arrays echoed back are not kept twice
    assert detail["group"] == {"id": 3056, "pnr": "4FMUAF - 4KOCE5", "price": "85000"}


def test_token_and_agent_from_env_skip_the_login_and_are_never_in_the_answer():
    supplier = Supplier()
    with patch.object(settings, "dhillu_sahi_token", SecretStr("env-token")), patch.object(settings, "dhillu_sahi_agent_id", 77):
        held = run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert supplier.paths() == ["/api/add-booking", "/api/agent-booking-detail"]
    assert supplier.requests[0][1]["agent"] == 77 and supplier.requests[0][2] == "Bearer env-token"
    assert "env-token" not in json.dumps(held.supplier_answer) and "env-token" not in repr(held)


def test_env_agent_that_differs_from_the_login_books_nothing():
    supplier = Supplier()
    with patch.object(settings, "dhillu_sahi_agent_id", 99), pytest.raises(Exception) as raised:
        run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert raised.value.error_code == "agent_identity_mismatch" and supplier.paths() == ["/api/login"]


@pytest.mark.parametrize("status, body", [(200, {"success": False, "message": "Record has been updated"}), (422, {"message": "sold out"})])
def test_clear_rejection_is_a_refusal(status, body):
    supplier = Supplier(add_status=status, add_body=body)
    with pytest.raises(group_holds.SupplierRefused):
        run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert supplier.paths().count("/api/add-booking") == 1


def test_timeout_is_unclear_and_never_retried():
    supplier = Supplier(add_error=httpx.ReadTimeout("synthetic"))
    with pytest.raises(Exception) as raised:
        run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert not isinstance(raised.value, group_holds.SupplierRefused)
    assert supplier.paths().count("/api/add-booking") == 1


# ---------- the hold through the service ----------

@pytest.fixture
def store_ok():
    calls = []
    with patch.object(store, "create_pending", MagicMock(side_effect=lambda **kw: calls.append(("create_pending", kw)) or ("b-1", "TRV-GRP-AB12CD"))), \
         patch.object(store, "confirm_hold", MagicMock(side_effect=lambda *a, **kw: calls.append(("confirm_hold", kw)))), \
         patch.object(store, "mark_hold_failed", MagicMock(side_effect=lambda *a: calls.append(("mark_hold_failed", a)))), \
         patch.object(flight_holds, "resolve_saved", lambda people, owner: (people, {})), \
         patch.object(flight_holds, "_save_people", lambda *a, **kw: []), \
         patch.object(group_holds, "read", AsyncMock(side_effect=lambda reference, viewer: MagicMock(booking_id=reference))):
        yield calls


def request_for(stock, **passenger):
    return GroupHoldRequest(ticket_id=UUID(group_ticket_id(stock[0].tickets[0])), adults=1,
                            passengers=[{"type": "ADT", "title": "MR", "first_name": "Test", "last_name": "Traveler", "date_of_birth": "1990-01-01",
                                         "id_type": "PSPT", "document_number": "TEST12345", "document_country": "PAK",
                                         "document_expiry": "2031-01-01", "nationality": "PAK", **passenger}],
                            contact={"full_name": "Test Traveler", "phone": "+92 300 1234567", "email": "t@example.invalid"})


def service_hold(supplier, fetch, req, viewer=ADMIN):
    with patch.dict(group_holds.PROVIDERS, {"dhillu_sahi": lambda: DhilluSahiHoldProvider(httpx.MockTransport(supplier))}):
        return asyncio.run(group_holds.hold(req, viewer, fetch=fetch))


def test_hold_stores_the_booking_id_and_supplier_answer_on_our_record(store_ok):
    stock = normalize_groups(rows())
    service_hold(Supplier(), AsyncMock(return_value=stock), request_for(stock))
    confirmed = dict(store_ok)["confirm_hold"]
    assert confirmed["pnr"] == "4FMUAF - 4KOCE5" and confirmed["supplier_ref"] == "27049"
    assert confirmed["supplier_response"]["supplier_answer"]["booking_id"] == 27049
    assert confirmed["supplier_response"]["source"]["supplier_group_id"] == "3056"
    assert confirmed["hold_expires_at"] - datetime.now(timezone.utc) <= timedelta(minutes=settings.group_hold_minutes)


def test_cnic_traveler_is_refused_before_anything_is_written_or_sent(store_ok):
    stock = normalize_groups(rows())
    supplier = Supplier()
    with pytest.raises(GroupHoldError) as raised:
        service_hold(supplier, AsyncMock(return_value=stock), request_for(stock, id_type="NATI", document_number="3520112345671"))
    assert raised.value.status == 422 and raised.value.field_errors[0][0] == ["passengers", 0, "document_number"]
    assert store_ok == [] and supplier.requests == []


@pytest.mark.parametrize("after, code, text", [
    (None, "fare_gone", "This group fare is no longer available. Search again."),
    ({"remaining": 0}, "not_enough_seats", "Only 0 seats left on this fare. Nothing was held."),
    ({"price": "90000", "updated_at": "2026-11-14T15:20:00.000000Z"}, "price_changed", "The price has changed to Rs 90,000 per adult."),
])
def test_rejection_searches_again_and_answers_with_the_existing_messages(store_ok, after, code, text):
    stock = normalize_groups(rows())
    fresh = [] if after is None else normalize_groups(rows(**after))
    if after and "updated_at" not in after:
        fresh = normalize_groups(rows(**after))
    fetch = AsyncMock(side_effect=[stock, fresh])
    supplier = Supplier(add_body={"success": False, "message": "Record has been updated"})
    with pytest.raises(GroupHoldError) as raised:
        service_hold(supplier, fetch, request_for(stock))
    assert raised.value.code == code and raised.value.message.startswith(text)
    assert fetch.await_count == 2 and [name for name, _ in store_ok] == ["create_pending", "mark_hold_failed"]
    assert supplier.paths().count("/api/add-booking") == 1


def test_switched_off_keeps_todays_disabled_hold():
    with patch.object(settings, "group_dhillu_sahi_hold_enabled", False):
        assert group_holds.provider_for("dhillu_sahi") is None
    assert isinstance(group_holds.provider_for("dhillu_sahi"), DhilluSahiHoldProvider)


# ---------- our expiry, release, notice, issue ----------

def booking(status="held", expires_in=timedelta(minutes=10), release=None):
    now = datetime.now(timezone.utc)
    return store.GroupBooking("b-1", "TRV-GRP-AB12CD", "4FMUAF - 4KOCE5", "dhillu_sahi", status, "not_issued", "paid", now + expires_in, "PKR",
                              85_000_00, 85_000_00, "27049", [], 1, 85_000_00, release or {}, {}), now


def test_held_view_notice_only_with_supplier_access_no_release_no_issue():
    record, now = booking()
    view = group_holds.build_view(record, None, ADMIN, now)
    assert view.status == "held" and view.supplier_notice == "Supplier hold ends by itself after 1 hour"
    assert view.can_release is False and view.can_issue_tickets is False
    assert view.issue_unavailable_reason == "Issuing is not available for this provider yet"
    assert group_holds.build_view(record, None, STAFF, now).supplier_notice is None
    expired, now = booking(expires_in=timedelta(minutes=-1), status="expired", release={"status": store.RELEASE_NOT_AVAILABLE})
    view = group_holds.build_view(expired, None, ADMIN, now)
    assert view.status == "expired" and view.supplier_notice == "Supplier hold ends by itself after 1 hour" and view.supplier_release_failed is False
    assert not view.can_record_payments


def test_release_is_refused_without_a_supplier_call():
    record, _ = booking()
    with patch.object(store, "expire_if_over", lambda reference, now, no_release=frozenset(): record), pytest.raises(GroupHoldError) as raised:
        asyncio.run(group_holds.release("TRV-GRP-AB12CD", ADMIN))
    assert raised.value.code == "release_not_supported"


def test_expiry_job_marks_expired_and_calls_no_release():
    seen = {}
    with patch.object(store, "expire_due", lambda now, no_release=frozenset(): seen.setdefault("no_release", no_release) and ["TRV-GRP-AB12CD"]), \
         patch.object(store, "releases_due", lambda limit: []):
        outcome = asyncio.run(group_hold_expiry.run_once())
    assert "dhillu_sahi" in seen["no_release"] and outcome["released"] == [] and outcome["failed"] == []
