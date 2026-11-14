"""Dhillu & Sahi hold, with the supplier's HTTP mocked (no live call). Shapes from the Dhillu & Sahi APIs Integration
Manual: login {"success": {"token", "agent_id", "user_id", ...}}; check-availbility {"success": true, "data": {"status":
bool}, "message"}; add-booking {"success": true, "data": {"id": <int>}, "message"}; agent-booking-detail {"success": true,
"data": {"data": {id, agent, pnr, booking_date, confirmed, total_amount, final_total, received_amount, remaining_amount,
cancel_status, cancel_at, booking_sub[], payments[]}, "groupdata": {id, pnr, price, airline, subpart[]}}, "message"}.
No release call exists: nothing is released at our expiry."""

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


def detail_booking(total="85000", cancel_status="0", confirmed="0", received="0", cancel_at=None):
    return {"id": 27049, "agent": "41", "pnr": "4FMUAF - 4KOCE5", "booking_date": "2026-11-14 15:09:58", "confirmed": confirmed,
            "added_by": {"id": 1, "name": "Admin"}, "edit_by": None, "cancel_by": None, "total_amount": total, "final_total": total,
            "received_amount": received, "remaining_amount": total, "cancel_status": cancel_status, "cancel_at": cancel_at,
            "pdf_download_link": "https://supplier.invalid/api/booking-pdf/27049?id=41", "pdf_view_link": "https://supplier.invalid/api/booking-pdf-view/27049?id=41",
            "booking_sub": [{"id": 4201, "booking_id": "27049", "title": "Mr", "firstname": "TEST", "lastname": "TRAVELER", "dob": "1990-01-01",
                             "passport_no": "TEST12345", "passport_expiry_date": "2031-01-01", "type": "adult", "passport_img": None, "visa_copy": None}],
            "payments": []}


class Supplier:
    """Answers login, check-availbility, add-booking and agent-booking-detail with the documented shapes; records every
    request (path, JSON body, auth header)."""

    def __init__(self, add_status=200, add_body=None, add_error=None, available=True, detail=None):
        self.requests = []
        self.add_status, self.add_error, self.available = add_status, add_error, available
        self.add_body = add_body if add_body is not None else {"success": True, "link": None, "data": {"id": 27049}, "message": "Booking Added Successful"}
        self.detail = detail or detail_booking()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.url.path, body, request.headers.get("authorization")))
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"success": {"token": "login-token", "name": "Travelian", "username": "travelian", "email": "a@example.invalid",
                                                         "image": None, "role": "agent", "agent_id": 41, "user_id": 5454545,
                                                         "change_password": False, "w_verified": False}})
        if request.url.path == "/api/check-availbility":
            return httpx.Response(200, json={"success": True, "link": None, "data": {"status": self.available}, "message": "Check availbility"})
        if request.url.path == "/api/add-booking":
            if self.add_error:
                raise self.add_error
            return httpx.Response(self.add_status, json=self.add_body)
        if request.url.path == "/api/agent-booking-detail":
            return httpx.Response(200, json={"success": True, "link": None, "message": "Booking Details Fetch Successful", "data": {
                "data": self.detail,
                "groupdata": {"id": 3056, "pnr": "4FMUAF - 4KOCE5", "price": "85000", "airline": {"id": 20, "short_name": "PF", "logo": None}, "subpart": []}}})
        return httpx.Response(404, json={})

    def paths(self):
        return [path for path, _, _ in self.requests]


@pytest.fixture(autouse=True)
def configured():
    with patch.object(settings, "dhillu_sahi_email", "a@example.invalid"), patch.object(settings, "dhillu_sahi_password", SecretStr("synthetic")), \
         patch.object(settings, "dhillu_sahi_token", None), patch.object(settings, "dhillu_sahi_agent_id", None), \
         patch.object(settings, "group_dhillu_sahi_hold_enabled", True), \
         patch("app.vendors.group_ticketing.dhillu_sahi.holds.DETAIL_RETRY_SECONDS", 0), \
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


def test_titles_map_to_the_documented_ones():
    body = booking_payload(ticket(), [adult(title="MR"), adult(title="MRS"), adult(title="MISS"), adult(title="MASTER")], agent=41)
    assert body["titles"] == ["MR", "MRS", "MISS", "MSTR"]
    assert set(body["titles"]) <= {"MR", "MRS", "MS", "MSTR", "MISS"}


def test_a_title_add_booking_does_not_take_is_a_field_error():
    odd = adult().model_copy(update={"title": "DR"})
    assert missing_traveler_details([adult(), odd]) == [(1, "title")]
    with pytest.raises(ValueError):
        booking_payload(ticket(), [odd], agent=41)


# ---------- provider ----------

def run(coro):
    return asyncio.run(coro)


def test_hold_checks_availability_books_once_then_reads_the_detail():
    supplier = Supplier()
    held = run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert supplier.paths() == ["/api/login", "/api/check-availbility", "/api/add-booking", "/api/agent-booking-detail"]
    assert supplier.requests[1][1] == {"pnr": "4FMUAF - 4KOCE5", "adult": 1, "child": 0, "price": 85000}  # the listing price per seat
    _, sent, auth = supplier.requests[2]
    assert sent["pnr"] == "4FMUAF - 4KOCE5" and sent["updated_at"] == UPDATED and auth == "Bearer login-token"
    assert sent["agent"] == 41  # the login's agent_id, never its user_id (5454545)
    assert supplier.requests[3][1] == {"booking_id": 27049}
    assert (held.pnr, held.supplier_ref, held.cost_per_adult.amount_minor) == ("4FMUAF - 4KOCE5", "27049", 85_000_00)
    assert held.supplier_total.amount_minor == 85_000_00  # total_amount, PKR
    assert held.supplier_answer["amounts"] == {"total_amount": "85000", "final_total": "85000", "received_amount": "0", "remaining_amount": "85000",
                                               "cancel_status": "0", "confirmed": "0", "cancel_at": None, "booking_date": "2026-11-14 15:09:58"}
    assert "TEST12345" not in json.dumps(held.supplier_answer)  # traveler arrays echoed back are not kept twice


def test_availability_false_books_nothing():
    supplier = Supplier(available=False)
    with pytest.raises(group_holds.PriceOrSeatsChanged):
        run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert "/api/add-booking" not in supplier.paths()


def test_token_and_agent_from_env_skip_the_login_and_are_never_in_the_answer():
    supplier = Supplier()
    with patch.object(settings, "dhillu_sahi_token", SecretStr("env-token")), patch.object(settings, "dhillu_sahi_agent_id", 77):
        held = run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert supplier.paths() == ["/api/check-availbility", "/api/add-booking", "/api/agent-booking-detail"]
    assert supplier.requests[1][1]["agent"] == 77 and supplier.requests[1][2] == "Bearer env-token"
    assert "env-token" not in json.dumps(held.supplier_answer) and "env-token" not in repr(held)


def test_env_agent_id_is_used_when_set_otherwise_the_logins():
    supplier = Supplier()
    with patch.object(settings, "dhillu_sahi_agent_id", 99):
        run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).hold(ticket(), [adult()], CONTACT))
    assert next(body for path, body, _ in supplier.requests if path == "/api/add-booking")["agent"] == 99


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
    kept = confirmed["supplier_response"]["supplier_amounts"]
    assert {k: kept[k] for k in ("total_amount", "final_total", "received_amount", "remaining_amount", "cancel_status")} == {
        "total_amount": "85000", "final_total": "85000", "received_amount": "0", "remaining_amount": "85000", "cancel_status": "0"}
    assert "price_change" not in confirmed["supplier_response"] and confirmed["selling_total_minor"] == 85_000_00
    left = confirmed["hold_expires_at"] - datetime.now(timezone.utc)
    assert timedelta(minutes=44) < left <= timedelta(minutes=45)  # DHILLU_SAHI_HOLD_DEFAULT_MINUTES, not GROUP_HOLD_MINUTES
    assert confirmed["supplier_response"]["hold_time"]["source"] == "default" and confirmed["supplier_response"]["hold_time"]["minutes"] == 45


def test_held_total_different_from_the_shown_price_needs_the_customer_to_agree(store_ok):
    stock = normalize_groups(rows())
    service_hold(Supplier(detail=detail_booking(total="90000")), AsyncMock(return_value=stock), request_for(stock))
    confirmed = dict(store_ok)["confirm_hold"]
    assert confirmed["selling_total_minor"] == 90_000_00 and confirmed["supplier_total_minor"] == 90_000_00  # the held price
    assert confirmed["supplier_response"]["price_change"] == {"shown_total_minor": 85_000_00, "held_total_minor": 90_000_00, "accepted": False}


@pytest.mark.parametrize("available", [False])
def test_availability_false_searches_again_with_the_existing_messages(store_ok, available):
    stock = normalize_groups(rows())
    fetch = AsyncMock(side_effect=[stock, normalize_groups(rows(price="90000", updated_at="2026-11-14T15:20:00.000000Z"))])
    supplier = Supplier(available=available)
    with pytest.raises(GroupHoldError) as raised:
        service_hold(supplier, fetch, request_for(stock))
    assert raised.value.code == "price_changed" and "/api/add-booking" not in supplier.paths()
    assert [name for name, _ in store_ok] == ["create_pending", "mark_hold_failed"]


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

def booking(status="held", expires_in=timedelta(minutes=10), release=None, paid=85_000_00, price_change=None, amounts=None, ticket_id=None, hold_time=None):
    now = datetime.now(timezone.utc)
    return store.GroupBooking("b-1", "TRV-GRP-AB12CD", "4FMUAF - 4KOCE5", "dhillu_sahi", status, "not_issued", "paid" if paid else "unpaid",
                              now + expires_in, "PKR", 85_000_00, 85_000_00, "27049", [], 1, paid, release or {}, {}, {},
                              price_change or {}, amounts or {}, ticket_id, hold_time or {}), now


DEFAULT_HOLD_TIME = {"source": "default", "minutes": 45, "from": "2026-11-14T15:00:00+00:00"}


def test_hold_time_note_only_with_supplier_access_no_release_no_issue():
    record, now = booking(hold_time=DEFAULT_HOLD_TIME)
    view = group_holds.build_view(record, None, ADMIN, now)
    assert view.status == "held" and view.hold_time_note == "Hold time estimated (45 min default)"
    assert view.supplier_notice is None  # the old "Supplier hold ends by itself after 1 hour" is gone
    assert view.can_release is False and view.can_issue_tickets is False
    assert view.issue_unavailable_reason == "Issuing is not available for this provider yet"
    staff = group_holds.build_view(record, None, STAFF, now)
    assert staff.hold_time_note is None and staff.supplier_notice is None and staff.hold_expires_at == record.hold_expires_at
    expired, now = booking(expires_in=timedelta(minutes=-1), status="expired", release={"status": store.RELEASE_NOT_AVAILABLE}, hold_time=DEFAULT_HOLD_TIME)
    view = group_holds.build_view(expired, None, ADMIN, now)
    assert view.status == "expired" and view.hold_time_note == "Hold time estimated (45 min default)" and view.supplier_release_failed is False
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


# ---------- price change, supplier amounts, cancellation ----------

def test_price_change_shows_until_agreed_and_blocks_payments():
    record, now = booking(paid=0, price_change={"shown_total_minor": 80_000_00, "held_total_minor": 85_000_00, "accepted": False})
    view = group_holds.build_view(record, None, ADMIN, now)
    assert view.price_change.model_dump() == {"shown_total": {"amount_minor": 80_000_00, "currency": "PKR"},
                                              "held_total": {"amount_minor": 85_000_00, "currency": "PKR"},
                                              "difference": {"amount_minor": 5_000_00, "currency": "PKR"}}
    assert view.can_record_payments is False
    agreed, now = booking(paid=0, price_change={"shown_total_minor": 80_000_00, "held_total_minor": 85_000_00, "accepted": True})
    view = group_holds.build_view(agreed, None, ADMIN, now)
    assert view.price_change is None and view.can_record_payments is True


def test_confirmed_1_is_not_read_as_paid_or_ticketed():
    record, now = booking(paid=0, amounts={"confirmed": "1", "cancel_status": "0", "received_amount": "0"})
    view = group_holds.build_view(record, None, ADMIN, now)
    assert view.payment_status == "unpaid" and view.status == "held" and view.can_issue_tickets is False


def refresh_with(detail):
    supplier = Supplier(detail=detail)
    saved = {}

    def save(reference, booking_amounts, cancelled, now, supplier_expiry=None):
        saved.update(amounts=booking_amounts, cancelled=cancelled, supplier_expiry=supplier_expiry)
        expired, _ = booking(status="expired" if cancelled else "held", amounts=booking_amounts)
        return expired

    record, _ = booking(paid=0)
    with patch.dict(group_holds.PROVIDERS, {"dhillu_sahi": lambda: DhilluSahiHoldProvider(httpx.MockTransport(supplier))}), \
         patch.object(store, "save_supplier_amounts", save):
        after = run(group_holds.refresh_supplier(record))
    return after, saved, supplier


def test_cancel_status_1_on_a_later_read_expires_ours_and_blocks_payment_and_issuing():
    after, saved, supplier = refresh_with(detail_booking(cancel_status="1"))
    assert supplier.paths()[-1] == "/api/agent-booking-detail" and saved["cancelled"] is True
    assert saved["amounts"]["cancel_status"] == "1"
    view = group_holds.build_view(after, None, ADMIN, datetime.now(timezone.utc))
    assert view.status == "expired" and not view.can_record_payments and not view.can_issue_tickets
    assert view.supplier_notice == "Cancelled at the supplier"
    assert group_holds.build_view(after, None, STAFF, datetime.now(timezone.utc)).supplier_notice is None
    with patch.object(store, "expire_if_over", lambda reference, now, no_release=frozenset(): after), pytest.raises(GroupHoldError) as raised:
        run(group_holds.price_check("TRV-GRP-AB12CD", ADMIN))
    assert raised.value.code in ("hold_expired", "supplier_cancelled")


def test_active_booking_on_a_later_read_keeps_the_amounts_only():
    after, saved, _ = refresh_with(detail_booking(cancel_status="0", received="10000"))
    assert saved["cancelled"] is False and saved["amounts"]["received_amount"] == "10000" and after.booking_status == "held"


def test_price_check_before_issue_checks_availability_then_the_held_total():
    supplier = Supplier(detail=detail_booking(total="90000"))
    held = group_holds.HeldRef("4FMUAF - 4KOCE5", "27049", 1, group_holds.GroupMoney(amount_minor=85_000_00, currency="PKR"))
    cost = run(DhilluSahiHoldProvider(httpx.MockTransport(supplier)).price(held))
    assert cost.amount_minor == 90_000_00
    assert supplier.requests[1][1] == {"pnr": "4FMUAF - 4KOCE5", "adult": 1, "child": 0, "price": 85000}
    with pytest.raises(group_holds.PriceOrSeatsChanged):
        run(DhilluSahiHoldProvider(httpx.MockTransport(Supplier(available=False))).price(held))


def test_price_check_availability_false_searches_again():
    record, _ = booking(paid=0, ticket_id=group_ticket_id(ticket()))
    supplier = Supplier(available=False)
    with patch.dict(group_holds.PROVIDERS, {"dhillu_sahi": lambda: DhilluSahiHoldProvider(httpx.MockTransport(supplier))}), \
         patch.object(store, "expire_if_over", lambda reference, now, no_release=frozenset(): record), \
         patch.object(store, "save_supplier_amounts", lambda reference, a, cancelled, now, supplier_expiry=None: record), \
         patch("app.services.group_ticketing.fetch_all_group_inventory", AsyncMock(return_value=normalize_groups(rows(remaining=0)))), \
         pytest.raises(GroupHoldError) as raised:
        run(group_holds.price_check("TRV-GRP-AB12CD", ADMIN))
    assert raised.value.code == "not_enough_seats" and "Nothing was changed" in raised.value.message


# ---------- hold time: hold time + DHILLU_SAHI_HOLD_DEFAULT_MINUTES ----------

def test_note_shows_the_configured_default_minutes(store_ok):
    stock = normalize_groups(rows())
    with patch.object(settings, "dhillu_sahi_hold_default_minutes", 30):
        service_hold(Supplier(), AsyncMock(return_value=stock), request_for(stock))  # no cancel_at: the default applies
    hold_time = dict(store_ok)["confirm_hold"]["supplier_response"]["hold_time"]
    assert hold_time == {**hold_time, "source": "default", "minutes": 30}
    record, now = booking(hold_time=hold_time)
    assert group_holds.build_view(record, None, ADMIN, now).hold_time_note == "Hold time estimated (30 min default)"


def test_hold_time_follows_the_setting_and_other_suppliers_keep_group_hold_minutes(store_ok):
    stock = normalize_groups(rows())
    with patch.object(settings, "dhillu_sahi_hold_default_minutes", 50):
        service_hold(Supplier(), AsyncMock(return_value=stock), request_for(stock))
    confirmed = dict(store_ok)["confirm_hold"]
    assert timedelta(minutes=49) < confirmed["hold_expires_at"] - datetime.now(timezone.utc) <= timedelta(minutes=50)
    assert confirmed["supplier_response"]["hold_time"]["minutes"] == 50
    assert group_holds.hold_minutes(None) == settings.group_hold_minutes == 30
    assert group_holds.hold_minutes(DhilluSahiHoldProvider()) == 45


def test_detail_read_failing_after_add_booking_is_retried_and_keeps_the_booking_id(store_ok):
    stock = normalize_groups(rows())
    supplier = Supplier()
    original = supplier.__call__

    def failing_detail(request):
        if request.url.path == "/api/agent-booking-detail":
            supplier.requests.append((request.url.path, None, None))
            return httpx.Response(500, json={})
        return original(request)

    with patch("app.vendors.group_ticketing.dhillu_sahi.holds.DETAIL_RETRY_SECONDS", 0), \
         patch.dict(group_holds.PROVIDERS, {"dhillu_sahi": lambda: DhilluSahiHoldProvider(httpx.MockTransport(failing_detail))}):
        asyncio.run(group_holds.hold(request_for(stock), ADMIN, fetch=AsyncMock(return_value=stock)))
    assert supplier.paths().count("/api/agent-booking-detail") == 3 and supplier.paths().count("/api/add-booking") == 1
    confirmed = dict(store_ok)["confirm_hold"]
    assert confirmed["supplier_ref"] == "27049" and confirmed["supplier_response"]["supplier_answer"]["booking_id"] == 27049
    assert confirmed["supplier_response"]["hold_time"]["source"] == "default"  # the 45-minute fallback from the hold time
    assert timedelta(minutes=44) < confirmed["hold_expires_at"] - datetime.now(timezone.utc) <= timedelta(minutes=45)
    assert confirmed["selling_total_minor"] == 85_000_00 and "price_change" not in confirmed["supplier_response"]


def test_detail_read_succeeding_on_a_retry_keeps_its_amounts():
    supplier = Supplier()
    original, failures = supplier.__call__, []

    def flaky(request):
        if request.url.path == "/api/agent-booking-detail" and not failures:
            failures.append(1)
            return httpx.Response(502, json={})
        return original(request)

    with patch("app.vendors.group_ticketing.dhillu_sahi.holds.DETAIL_RETRY_SECONDS", 0):
        held = run(DhilluSahiHoldProvider(httpx.MockTransport(flaky)).hold(ticket(), [adult()], CONTACT))
    assert held.supplier_answer["amounts"]["total_amount"] == "85000" and held.supplier_total.amount_minor == 85_000_00


def test_amounts_as_numbers_are_accepted():
    numbers = {**detail_booking(), "total_amount": 90000, "final_total": 90000, "received_amount": 0, "remaining_amount": 90000,
               "cancel_status": 0, "confirmed": 0, "agent": 4569}
    held = run(DhilluSahiHoldProvider(httpx.MockTransport(Supplier(detail=numbers))).hold(ticket(), [adult()], CONTACT))
    assert held.supplier_total.amount_minor == 90_000_00 and held.supplier_answer["amounts"]["cancel_status"] == 0
    after, saved, _ = refresh_with({**numbers, "cancel_status": 1})
    assert saved["cancelled"] is True and after.booking_status == "expired"
    record, now = booking(paid=0, amounts={"confirmed": 1, "cancel_status": 0, "received_amount": 0})
    view = group_holds.build_view(record, None, ADMIN, now)
    assert view.payment_status == "unpaid" and view.status == "held"  # confirmed 1 (number) is not paid either


def test_expiry_reached_reads_as_expired_with_no_release_and_refuses_payment_and_issuing():
    record, now = booking(paid=0, expires_in=timedelta(seconds=-1), hold_time=DEFAULT_HOLD_TIME)
    view = group_holds.build_view(record, None, ADMIN, now)
    assert view.status == "expired" and not view.can_record_payments and not view.can_issue_tickets and not view.can_release
    with patch.object(store, "expire_if_over", lambda reference, now, no_release=frozenset(): record), pytest.raises(GroupHoldError) as raised:
        run(group_holds.issue("TRV-GRP-AB12CD", ADMIN))
    assert raised.value.code in ("hold_expired", "issue_not_supported")
    with patch.object(store, "expire_if_over", lambda reference, now, no_release=frozenset(): record), pytest.raises(GroupHoldError) as raised:
        run(group_holds.price_check("TRV-GRP-AB12CD", ADMIN))
    assert raised.value.code == "hold_expired"


# ---------- hold expiry: cancel_at (Asia/Karachi) when sane, else hold time + 45 minutes ----------

from zoneinfo import ZoneInfo

from app.vendors.group_ticketing.dhillu_sahi.holds import parse_cancel_at, supplier_expiry

PKT = ZoneInfo("Asia/Karachi")


def pkt(delta):
    """cancel_at as the supplier writes it: Pakistan local time, no zone."""
    return (datetime.now(timezone.utc) + delta).astimezone(PKT).strftime("%Y-%m-%d %H:%M:%S")


def test_cancel_at_is_pakistan_time_converted_to_utc():
    # Booking 34895: booking_date 16:10:45 UTC, cancel_at 21:50:45 PKT = 16:50:45 UTC, 40 minutes later.
    assert parse_cancel_at("2026-11-14 21:50:45") == datetime(2026, 11, 14, 16, 50, 45, tzinfo=timezone.utc)
    booked = datetime(2026, 11, 14, 16, 10, 45, tzinfo=timezone.utc)
    assert supplier_expiry({"cancel_at": "2026-11-14 21:50:45", "cancel_status": 0}, booked, 34895) - booked == timedelta(minutes=40)


@pytest.mark.parametrize("cancel_at, why", [
    (None, "missing"), ("", "missing"), ("14/11/2026 21:50", "unparsable"), ("not a date", "unparsable"),
    ("PAST", "in the past"), ("SOON", "under 5 minutes"), ("FAR", "over 2 hours"),
])
def test_unusable_cancel_at_falls_back_with_a_warning_naming_the_booking(cancel_at, why, caplog):
    value = {"PAST": pkt(-timedelta(minutes=10)), "SOON": pkt(timedelta(minutes=2)), "FAR": pkt(timedelta(hours=3))}.get(cancel_at, cancel_at)
    assert supplier_expiry({"cancel_at": value, "cancel_status": "0"}, datetime.now(timezone.utc), 27049) is None
    warning = next(r for r in caplog.records if r.getMessage().startswith("Dhillu & Sahi cancel_at not used"))
    assert warning.booking_id == 27049 and warning.cancel_at == value and warning.booked_at


def test_cancelled_booking_cancel_at_is_never_an_expiry():
    assert supplier_expiry({"cancel_at": pkt(timedelta(minutes=40)), "cancel_status": 1}, datetime.now(timezone.utc), 27049) is None


def test_sane_supplier_expiry_is_the_hold_expiry(store_ok):
    stock = normalize_groups(rows())
    service_hold(Supplier(detail=detail_booking(cancel_at=pkt(timedelta(minutes=40)))), AsyncMock(return_value=stock), request_for(stock))
    confirmed = dict(store_ok)["confirm_hold"]
    assert timedelta(minutes=39) < confirmed["hold_expires_at"] - datetime.now(timezone.utc) <= timedelta(minutes=40)
    assert confirmed["supplier_response"]["hold_time"]["source"] == "supplier"


@pytest.mark.parametrize("cancel_at", [None, "garbage", "PAST", "FAR"])
def test_unusable_supplier_expiry_uses_hold_time_plus_45(store_ok, cancel_at):
    value = {"PAST": pkt(-timedelta(minutes=10)), "FAR": pkt(timedelta(hours=3))}.get(cancel_at, cancel_at)
    stock = normalize_groups(rows())
    service_hold(Supplier(detail=detail_booking(cancel_at=value)), AsyncMock(return_value=stock), request_for(stock))
    confirmed = dict(store_ok)["confirm_hold"]
    assert timedelta(minutes=44) < confirmed["hold_expires_at"] - datetime.now(timezone.utc) <= timedelta(minutes=45)
    assert confirmed["supplier_response"]["hold_time"]["source"] == "default" and confirmed["supplier_response"]["hold_time"]["minutes"] == 45


def test_supplier_time_note_for_supplier_access_only():
    record, now = booking(hold_time={"source": "supplier", "from": "2026-11-14T15:00:00+00:00"})
    assert group_holds.build_view(record, None, ADMIN, now).hold_time_note == "Hold time from supplier"
    assert group_holds.build_view(record, None, STAFF, now).hold_time_note is None


def test_a_later_read_switches_a_default_hold_to_the_supplier_expiry():
    booked = datetime.now(timezone.utc) - timedelta(minutes=5)
    detail = detail_booking(cancel_at=pkt(timedelta(minutes=35)))
    supplier, saved = Supplier(detail=detail), {}
    record, _ = booking(paid=0, hold_time={"source": "default", "minutes": 45, "from": booked.isoformat()})

    def save(reference, booking_amounts, cancelled, now, supplier_expiry=None):
        saved.update(supplier_expiry=supplier_expiry)
        return record

    with patch.dict(group_holds.PROVIDERS, {"dhillu_sahi": lambda: DhilluSahiHoldProvider(httpx.MockTransport(supplier))}), \
         patch.object(store, "save_supplier_amounts", save):
        run(group_holds.refresh_supplier(record))
    assert timedelta(minutes=34) < saved["supplier_expiry"] - datetime.now(timezone.utc) <= timedelta(minutes=35)


def test_a_supplier_hold_time_is_not_switched_again():
    record, _ = booking(paid=0, hold_time={"source": "supplier", "from": datetime.now(timezone.utc).isoformat()})
    _, saved, _ = refresh_with(detail_booking(cancel_at=pkt(timedelta(minutes=20))))
    assert saved["supplier_expiry"] is None  # refresh_with's booking has no hold_time: nothing to switch
    supplier, seen = Supplier(detail=detail_booking(cancel_at=pkt(timedelta(minutes=20)))), {}
    with patch.dict(group_holds.PROVIDERS, {"dhillu_sahi": lambda: DhilluSahiHoldProvider(httpx.MockTransport(supplier))}), \
         patch.object(store, "save_supplier_amounts", lambda reference, a, c, now, supplier_expiry=None: seen.update(e=supplier_expiry) or record):
        run(group_holds.refresh_supplier(record))
    assert seen["e"] is None
