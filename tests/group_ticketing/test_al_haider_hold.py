"""Al Haider hold through create/booking, mocked only (never called live). Assumed shapes (the collection documents
the create answer only as "status" and "booking_id"): user -> {id, name, email, mobile_no, city} (read live);
create/booking -> {"status": true, "booking_id": <int>};
show/booking -> the documented booking object (id, group_id, adults, fares, status, auto_cancel_after, passengers, ...)."""

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
from app.services import flight_holds, group_holds
from app.services.group_holds import GroupHoldError, SupplierRefused
from app.services.group_ticketing import group_ticket_id
from app.services.group_ticketing_view import ticket_view
from app.vendors.group_ticketing.al_haider.holds import AlHaiderHoldProvider, agency_from_profile, booking_id_of, booking_payload
from app.vendors.group_ticketing.al_haider.inventory import normalize
from tests.group_ticketing.test_al_haider_and_sectors import al_group

ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))
STAFF = BackOfficeUser("u-2", "staff", NONE)
TOKEN = "synthetic-permanent-token-0123456789"
PROFILE = {"id": 3047, "name": "Travelian (Private) Limited", "email": "ops@example.invalid", "mobile_no": "03000000000", "city": "Lahore",
           "email_verified_at": None}
AGENCY = {"agent_name": "Travelian (Private) Limited", "agency_name": "Travelian (Private) Limited", "email": "ops@example.invalid", "mobile": "03000000000"}
CONTACT = HoldContact(first_name="Test", last_name="Traveler", email="t@example.invalid", phone_code="92", phone="3001234567")
SHOWN = {"id": 21319, "group_id": 3437, "agency_id": 9, "agency_name": "Travelian (Private) Limited", "agent_name": "Ops Desk", "email": "ops@example.invalid",
         "mobile": "03000000000", "adults": 1, "child": 0, "infant": 0, "fares": "98000", "status": 1, "auto_cancel_after": "2026-11-14 18:00:00",
         "sold_at": None, "canceled_at": None, "created_at": "2026-11-14 16:00:00",
         "passengers": [{"surname": "Traveler", "given_name": "Test", "passport_no": "TEST12345"}],
         "booking_details": [{"surname": "Traveler", "given_name": "Test", "passport_no": "TEST12345"}], "created_by": {"email": "x@example.invalid"}}


def adult(**kw):
    return HoldPassenger.model_validate({"type": "ADT", "title": "MR", "first_name": "Test", "last_name": "Traveler", "date_of_birth": "1990-01-01",
                                         "id_type": "PSPT", "document_number": "TEST12345", "document_country": "PAK", "document_expiry": "2031-01-01",
                                         "nationality": "PAK", **kw})


@pytest.fixture(autouse=True)
def configured():
    with patch.object(settings, "al_haider_base_url", "https://al-haider.invalid"), patch.object(settings, "al_haider_access_token", SecretStr(TOKEN)), \
         patch.object(settings, "group_al_haider_hold_enabled", True), \
         patch.object(settings, "group_ticketing_markup_percent", Decimal("0")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0):
        yield


def ticket():
    return normalize([al_group()])[0].tickets[0]


class Supplier:
    def __init__(self, status=200, body=None, error=None, profile=None):
        self.status, self.error, self.requests = status, error, []
        self.profile = PROFILE if profile is None else profile
        self.body = body if body is not None else {"status": True, "booking_id": 21319}

    def __call__(self, request):
        self.requests.append((request.method, request.url.path, json.loads(request.content) if request.content else None, request.headers.get("authorization")))
        if request.url.path == "/api/user":
            return self.profile if isinstance(self.profile, httpx.Response) else httpx.Response(200, json=self.profile)
        if request.url.path == "/api/create/booking":
            if self.error:
                raise self.error
            return httpx.Response(self.status, json=self.body)
        if request.url.path.startswith("/api/show/booking/"):
            return httpx.Response(200, json=SHOWN)
        return httpx.Response(404, json={})

    def calls(self):
        return [(method, path) for method, path, _, _ in self.requests]


def run(coro):
    return asyncio.run(coro)


# ---------- payload ----------

def test_payload_group_id_agency_info_and_adults():
    body = booking_payload(ticket(), [adult(), adult(title="MISS", first_name="Sana", document_number="CD7654321", date_of_birth="1995-02-03")], AGENCY)
    assert body == {
        "group_id": 3437,
        "agency_info": {"group_id": 3437, "agent_name": "Travelian (Private) Limited", "agency_name": "Travelian (Private) Limited", "email": "ops@example.invalid",
                        "mobile": "03000000000", "adults": 2, "child": 0, "infant": 0, "agent_notes": None},
        "booking_details": [
            {"type": "Adult", "surname": "Traveler", "given_name": "Test", "title": "MR", "passport_no": "TEST12345", "dob": "1990-01-01", "doe": "2031-01-01"},
            {"type": "Adult", "surname": "Traveler", "given_name": "Sana", "title": "MS", "passport_no": "CD7654321", "dob": "1995-02-03", "doe": "2031-01-01"},
        ],
    }


def test_payload_refuses_children_and_missing_passport():
    with pytest.raises(ValueError):
        booking_payload(ticket(), [adult(type="CHD")], AGENCY)
    with pytest.raises(ValueError):
        booking_payload(ticket(), [adult(document_expiry=None)], AGENCY)


def test_agency_info_from_the_account_profile():
    assert agency_from_profile(PROFILE) == AGENCY
    assert agency_from_profile({"data": PROFILE}) == AGENCY
    for missing in ("name", "email", "mobile_no"):
        with pytest.raises(Exception) as raised:
            agency_from_profile({**PROFILE, missing: ""})
        assert raised.value.error_code == "profile_incomplete"


@pytest.mark.parametrize("answer, booking_id", [({"status": True, "booking_id": 21319}, 21319), ({"status": "success", "data": {"booking_id": "21319"}}, 21319),
                                                ({"success": True, "data": {"id": 21319}}, 21319), ({"status": True}, None), ([], None)])
def test_booking_id_read_from_the_assumed_shapes(answer, booking_id):
    assert booking_id_of(answer) == booking_id


# ---------- provider ----------

def test_hold_posts_once_with_the_bearer_token_then_reads_the_booking():
    supplier = Supplier()
    held = run(AlHaiderHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert supplier.calls() == [("GET", "/api/user"), ("POST", "/api/create/booking"), ("GET", "/api/show/booking/21319")]
    assert all(auth == f"Bearer {TOKEN}" for _, _, _, auth in supplier.requests)
    assert supplier.requests[1][2]["group_id"] == 3437 and supplier.requests[1][2]["agency_info"]["mobile"] == "03000000000"
    assert (held.pnr, held.supplier_ref, held.cost_per_adult.amount_minor) == ("4UD1CN-49Z0AJ", "21319", 98_000_00)
    detail = held.supplier_answer["detail"]
    assert detail["auto_cancel_after"] == "2026-11-14 18:00:00" and detail["fares"] == "98000" and detail["status"] == 1
    assert "passengers" not in detail and "booking_details" not in detail and "created_by" not in detail  # traveler data not copied again
    assert TOKEN not in json.dumps(held.supplier_answer) and TOKEN not in repr(held)


@pytest.mark.parametrize("status, body", [(422, {"message": "The given data was invalid."}), (200, {"status": False, "message": "Seats not available"}),
                                          (200, {"sucess": False})])
def test_clear_rejection_is_a_refusal_and_sent_once(status, body):
    supplier = Supplier(status=status, body=body)
    with pytest.raises(SupplierRefused):
        run(AlHaiderHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert supplier.calls() == [("GET", "/api/user"), ("POST", "/api/create/booking")]


@pytest.mark.parametrize("profile", [{**PROFILE, "mobile_no": None}, httpx.Response(401, json={"message": "Unauthenticated."})])
def test_incomplete_or_unreadable_profile_books_nothing(profile):
    supplier = Supplier(profile=profile)
    with pytest.raises(Exception) as raised:
        run(AlHaiderHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert raised.value.error_code in ("profile_incomplete", "profile_unavailable") and not isinstance(raised.value, SupplierRefused)
    assert supplier.calls() == [("GET", "/api/user")]


@pytest.mark.parametrize("supplier", [Supplier(error=httpx.ReadTimeout("synthetic")), Supplier(status=500, body={}), Supplier(body={"status": True})])
def test_unclear_answers_are_not_refusals_and_never_retried(supplier):
    with pytest.raises(Exception) as raised:
        run(AlHaiderHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert not isinstance(raised.value, SupplierRefused)
    assert supplier.calls().count(("POST", "/api/create/booking")) == 1


# ---------- flag and listing ----------

def test_flag_off_keeps_hold_disabled_with_the_reason():
    with patch.object(settings, "group_al_haider_hold_enabled", False):
        assert group_holds.provider_for("al_haider") is None
        view = ticket_view(ticket(), ALL)
        assert view.can_hold is False and view.hold_unavailable_reason == "Holding is not wired for Al Haider yet"
        assert ticket_view(ticket(), NONE).hold_unavailable_reason == "Holding isn't available for this provider"


def test_flag_on_needs_only_the_token_and_url():
    assert ticket_view(ticket(), ALL).can_hold is True
    with patch.object(settings, "al_haider_access_token", None):
        assert group_holds.provider_for("al_haider") is None
        assert ticket_view(ticket(), ALL).hold_unavailable_reason == "Holding is not wired for Al Haider yet"


# ---------- the hold through the service (record first) ----------

@pytest.fixture
def store_calls():
    calls = []
    with patch.object(store, "create_pending", MagicMock(side_effect=lambda **kw: calls.append(("create_pending", kw)) or ("b-1", "TRV-GRP-AB12CD"))), \
         patch.object(store, "confirm_hold", MagicMock(side_effect=lambda *a, **kw: calls.append(("confirm_hold", kw)))), \
         patch.object(store, "mark_hold_failed", MagicMock(side_effect=lambda *a: calls.append(("mark_hold_failed", a)))), \
         patch.object(store, "mark_unrecorded", MagicMock(side_effect=lambda *a, **kw: calls.append(("mark_unrecorded", kw)))), \
         patch.object(flight_holds, "resolve_saved", lambda people, owner: (people, {})), \
         patch.object(flight_holds, "_save_people", lambda *a, **kw: []), \
         patch.object(group_holds, "read", AsyncMock(side_effect=lambda reference, viewer: MagicMock(booking_id=reference))):
        yield calls


def hold_request(stock):
    return GroupHoldRequest(fare_terms_acknowledged=True, ticket_id=UUID(group_ticket_id(stock[0].tickets[0])), adults=1, passengers=[{
        "type": "ADT", "title": "MR", "first_name": "Test", "last_name": "Traveler", "date_of_birth": "1990-01-01", "id_type": "PSPT",
        "document_number": "TEST12345", "document_country": "PAK", "document_expiry": "2031-01-01", "nationality": "PAK"}],
        contact={"full_name": "Test Traveler", "phone": "+92 300 1234567", "email": "t@example.invalid"})


def service_hold(supplier, viewer=ADMIN):
    stock = normalize([al_group()])
    with patch.dict(group_holds.PROVIDERS, {"al_haider": lambda: AlHaiderHoldProvider(httpx.MockTransport(supplier))}):
        return run(group_holds.hold(hold_request(stock), viewer, fetch=AsyncMock(return_value=stock)))


def test_record_first_then_supplier_then_record_completed(store_calls):
    supplier = Supplier()
    service_hold(supplier)
    assert [name for name, _ in store_calls] == ["create_pending", "confirm_hold"]
    confirmed = store_calls[1][1]
    assert confirmed["pnr"] == "4UD1CN-49Z0AJ" and confirmed["supplier_ref"] == "21319"
    assert confirmed["supplier_response"]["supplier_answer"]["booking_id"] == 21319
    assert confirmed["supplier_response"]["source"]["supplier_group_id"] == "3437"


def test_save_failure_keeps_the_pnr_and_says_release_manually(store_calls):
    supplier = Supplier()
    with patch.object(store, "confirm_hold", MagicMock(side_effect=RuntimeError("update failed"))), pytest.raises(GroupHoldError) as raised:
        service_hold(supplier)
    unrecorded = dict(store_calls)["mark_unrecorded"]
    assert unrecorded["supplier_pnr"] == "4UD1CN-49Z0AJ" and unrecorded["release_pending"] is False
    assert "Supplier hold not released, release manually" in raised.value.message and "1 hour" not in raised.value.message
    assert ("PATCH", "/api/cancel/booking/21319") not in supplier.calls()


def test_rejection_marks_the_record_failed(store_calls):
    with pytest.raises(GroupHoldError) as raised:
        service_hold(Supplier(status=422, body={"message": "invalid"}))
    assert [name for name, _ in store_calls] == ["create_pending", "mark_hold_failed"] and raised.value.status == 409


# ---------- our expiry, release, notice, issue ----------

def booking(status="held", expires_in=timedelta(minutes=10)):
    now = datetime.now(timezone.utc)
    return store.GroupBooking("b-1", "TRV-GRP-AB12CD", "4UD1CN-49Z0AJ", "al_haider", status, "not_issued", "paid", now + expires_in, "PKR",
                              98_000_00, 98_000_00, "21319", [], 1, 98_000_00, {}, {}), now


def test_no_notice_until_the_supplier_confirms_its_expiry_no_release_no_issue():
    record, now = booking()
    view = group_holds.build_view(record, None, ADMIN, now)
    assert view.supplier_notice is None and view.can_release is False and view.can_issue_tickets is False
    assert view.issue_unavailable_reason == "Issuing is not available for this provider yet"
    unrecorded, now = booking(status=store.HOLD_UNRECORDED)
    assert group_holds.build_view(unrecorded, None, ADMIN, now).supplier_notice == "Supplier hold not released, release manually"


def test_expiry_sends_nothing_and_release_is_refused():
    assert "al_haider" in group_holds.NO_RELEASE_SOURCES
    record, _ = booking()
    with patch.object(store, "expire_if_over", lambda reference, now, no_release=frozenset(): record), pytest.raises(GroupHoldError) as raised:
        run(group_holds.release("TRV-GRP-AB12CD", ADMIN))
    assert raised.value.code == "release_not_supported"
