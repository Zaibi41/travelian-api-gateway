"""Saved customers and travelers (migration 0017): matching, scope, masking, duplicates, merge,
reveal, holds with saved people, and the backfill. Synthetic people only."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.routes import saved_people as routes
from app.api.routes.live_ticketing import router as live_router
from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, require_back_office, resolve_actions
from app.db import booking_people, saved_people as store
from app.schemas.flights import FlightSearchRequest
from app.services import flight_holds, live_flights
from tests.database.test_booking_people import ACTOR, save
from tests.southair.fixtures import pnr_info, search_data

ADMIN = BackOfficeUser(ACTOR, "super_admin", ALL, resolve_actions("super_admin", set()))
STAFF = BackOfficeUser(ACTOR, "staff", NONE)
CNIC = "35202-1234567-1"
PASSPORT = "ab1234567"


def person(owner=None, first="Test", last="Passenger", dob=date(1990, 1, 31), document=None, **extra):
    return store.create(owner, ACTOR, {"title": "MR", "first_name": first, "last_name": last, "date_of_birth": dob, **extra}, document)


def passport(number=PASSPORT, expiry=date(2031, 1, 1)):
    return store.DocumentInput("PSPT", "PAK", number, expiry)


def cnic(number=CNIC):
    return store.DocumentInput("NATI", "PAK", number)


@pytest.fixture
def db(database):
    connection, metadata, _ = database
    connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    connection.execute(text("INSERT INTO agencies (id, name, status) VALUES ('ag-1', 'Synthetic One', 'active'), ('ag-2', 'Synthetic Two', 'active')"))
    return connection, metadata


def api(viewer=ADMIN):
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    app.dependency_overrides[require_back_office] = lambda: viewer
    return TestClient(app)


# ---------- matching and scope ----------

def test_document_numbers_are_normalised_for_matching(db):
    saved, outcome = person(document=cnic(CNIC))
    assert outcome == "created"
    assert store.search(None, "3520212345671")[0].id == saved.id                       # CNIC without dashes
    assert store.search(None, "35202 1234567 1")[0].match == "document"
    other, _ = person(first="Other", dob=date(1985, 5, 5), document=passport(PASSPORT))
    assert [p.id for p in store.search(None, "AB-1234567")] == [other.id]              # passport, any case or punctuation
    connection, _ = db
    stored = connection.execute(text("SELECT document_number_normalized FROM saved_person_documents ORDER BY created_at")).scalars().all()
    assert stored == ["3520212345671", "AB1234567"]


def test_search_order_document_then_contact_then_name(db):
    by_doc, _ = person(first="Ayesha", last="Khan", document=passport())
    by_email, _ = person(first="Bilal", last="Khan", dob=date(1980, 2, 2), email="Khan.Family@example.invalid", phone_code="92", phone="3001234567")
    assert [p.match for p in store.search(None, "AB1234567")][:1] == ["document"]
    assert [p.id for p in store.search(None, "khan.family@example.invalid")] == [by_email.id]
    assert [p.id for p in store.search(None, "03001234567")] == [by_email.id]
    assert {p.id for p in store.search(None, "kha")} == {by_doc.id, by_email.id}
    assert [p.id for p in store.search(None, "ayesha kh")] == [by_doc.id]


def test_each_owner_sees_only_its_own_people_and_the_same_document_may_exist_once_per_owner(db):
    own, _ = person(None, document=passport())
    agency, _ = person("ag-1", document=passport())               # same number, another owner: allowed
    assert own.id != agency.id
    assert [p.id for p in store.search(None, "AB1234567")] == [own.id]
    assert [p.id for p in store.search("ag-1", "AB1234567")] == [agency.id]
    assert store.search("ag-2", "AB1234567") == [] and store.search("ag-2", "test") == []
    with pytest.raises(store.PeopleError) as error:
        store.get("ag-2", own.id)
    assert error.value.status == 404


def test_agency_and_customer_roles_get_403():
    from fastapi import HTTPException
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    def deny():
        raise HTTPException(403, "Back-office access denied.")
    app.dependency_overrides[require_back_office] = deny
    assert TestClient(app).get("/v1/admin/customers", params={"q": "test"}).status_code == 403


# ---------- masked API output ----------

def test_search_and_detail_are_masked_and_never_carry_full_numbers(db):
    saved, _ = person(document=passport(), email="ops@example.invalid", phone_code="92", phone="3001234567")
    store.add_document(None, ACTOR, saved.id, cnic())
    client = api()
    found = client.get("/v1/admin/customers", params={"q": "AB1234567"})
    detail = client.get(f"/v1/admin/customers/{saved.id}")
    for response in (found, detail):
        assert response.status_code == 200
        for value in ("AB1234567", "ab1234567", "3520212345671", CNIC, "1990-01-31", "2031-01-01", "3001234567", "ops@example.invalid"):
            assert value not in response.text, value
    result = found.json()["results"][0]
    assert result["birth_year"] == 1990 and result["phone_masked"] == "+92 •••••••567" and result["email_masked"] == "o•••@example.invalid"
    assert {d["document_number_masked"] for d in result["documents"]} == {"••••••567", "••••••••••671"}
    assert {d.get("document_validity") for d in result["documents"]} == {"valid", None}
    assert detail.json()["date_of_birth_masked"] == "1990-••-••" and detail.json()["can_reveal_identity"] is True


def test_search_needs_three_characters_and_returns_at_most_twenty(db):
    for index in range(22):
        person(first="Zain", last=f"Synthetic{chr(65 + index)}", dob=date(1990, 1, 1) + timedelta(days=index))
    client = api()
    assert client.get("/v1/admin/customers", params={"q": "za"}).status_code == 422
    assert len(client.get("/v1/admin/customers", params={"q": "zai"}).json()["results"]) == 20


# ---------- duplicates and conflicts ----------

def test_same_name_and_birth_date_with_another_document_is_a_possible_duplicate(db):
    first, _ = person(document=passport("AB1234567"))
    client = api()
    body = {"title": "MR", "first_name": "Test", "last_name": "Passenger", "date_of_birth": "1990-01-31",
            "document": {"id_type": "PSPT", "document_country": "PAK", "document_number": "CD7654321", "document_expiry": "2033-01-01"}}
    response = client.post("/v1/admin/customers", json=body)
    assert response.status_code == 409 and response.json()["code"] == "possible_duplicate"
    assert response.json()["possible_duplicate"]["id"] == first.id and "CD7654321" not in response.text and "AB1234567" not in response.text
    added = client.post("/v1/admin/customers", json={**body, "duplicate_choice": {"choice": "add_to_existing", "person_id": first.id}})
    assert added.status_code == 200 and added.json()["outcome"] == "added_to_existing"
    assert len(added.json()["person"]["documents"]) == 2
    separate = client.post("/v1/admin/customers", json={**body, "document": {**body["document"], "document_number": "EF1112223"},
                                                         "duplicate_choice": {"choice": "keep_separate", "person_id": first.id}})
    assert separate.status_code == 201 and separate.json()["person"]["id"] != first.id
    connection, _ = db
    choices = connection.execute(text("SELECT details->>'choice' FROM saved_person_events WHERE action = 'duplicate_choice' ORDER BY created_at")).scalars().all()
    assert choices == ["add_to_existing", "keep_separate"]


def test_a_document_saved_for_someone_else_is_refused_with_409_and_no_personal_data(db):
    person(document=passport())
    response = api().post("/v1/admin/customers", json={
        "first_name": "Different", "last_name": "Person", "date_of_birth": "1975-03-03",
        "document": {"id_type": "PSPT", "document_country": "PAK", "document_number": "AB 1234567", "document_expiry": "2030-01-01"}})
    assert response.status_code == 409 and response.json() == {
        "detail": "This document number is already saved for a different person.", "code": "document_belongs_to_another_person"}


def test_create_edit_and_documents_need_manage_people(db):
    saved, _ = person()
    client = api(STAFF)
    assert client.post("/v1/admin/customers", json={"first_name": "Test", "last_name": "Other"}).status_code == 403
    assert client.patch(f"/v1/admin/customers/{saved.id}", json={"phone": "3001112223"}).status_code == 403
    assert client.post(f"/v1/admin/customers/{saved.id}/documents", json={"id_type": "NATI", "document_country": "PAK", "document_number": CNIC}).status_code == 403
    granted = BackOfficeUser(ACTOR, "staff", NONE, frozenset({"manage_people"}))
    assert api(granted).post(f"/v1/admin/customers/{saved.id}/documents",
                             json={"id_type": "NATI", "document_country": "PAK", "document_number": CNIC}).status_code == 201


def test_remove_a_document(db):
    saved, _ = person(document=passport())
    doc = saved.documents[0].id
    response = api().delete(f"/v1/admin/customers/{saved.id}/documents/{doc}")
    assert response.status_code == 200 and response.json()["documents"] == []
    assert store.search(None, "AB1234567") == []


# ---------- merge and reveal ----------

def test_merge_moves_documents_and_booking_links_and_is_logged(db):
    connection, metadata = db
    keep, _ = person(document=passport())
    old, _ = person(first="Test", last="Passenger", dob=None, document=cnic(), email="ops@example.invalid")
    reference = save(connection, metadata)
    connection.execute(text("UPDATE booking_customers SET person_id = :p"), {"p": old.id})
    client = api(BackOfficeUser(ACTOR, "staff", NONE))
    assert client.post(f"/v1/admin/customers/{old.id}/merge", json={"merge_into_person_id": keep.id, "reason": "Same person"}).status_code == 403
    response = api().post(f"/v1/admin/customers/{old.id}/merge", json={"merge_into_person_id": keep.id, "reason": "Same person, two records"})
    assert response.status_code == 200 and len(response.json()["documents"]) == 2
    assert [b["booking_id"] for b in response.json()["bookings"]] == [reference]
    assert connection.scalar(text("SELECT merged_into_id FROM saved_people WHERE id = :p"), {"p": old.id}) == keep.id
    events = dict(connection.execute(text("SELECT action, details FROM saved_person_events WHERE action LIKE 'merged%'")).all())
    assert events["merged_into"]["reason"] == "Same person, two records" and events["merged_from"]["documents_moved"] == 1
    assert store.search(None, "3520212345671")[0].id == keep.id                 # the merged record no longer shows
    assert all(p.id != old.id for p in store.search(None, "test"))


def test_reveal_needs_the_permission_and_is_logged_without_values(db):
    connection, _ = db
    saved, _ = person(document=passport())
    assert api(STAFF).post(f"/v1/admin/customers/{saved.id}/reveal", json={"reason": "Ticketing needs it"}).status_code == 403
    assert api().post(f"/v1/admin/customers/{saved.id}/reveal", json={"reason": ""}).status_code == 422
    response = api().post(f"/v1/admin/customers/{saved.id}/reveal", json={"reason": "Ticketing needs it"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"date_of_birth": "1990-01-31", "documents": [{"id": saved.documents[0].id, "document_number": "ab1234567", "document_expiry": "2031-01-01"}]}
    details = connection.execute(text("SELECT details FROM saved_person_events WHERE action = 'identity_revealed'")).scalar()
    assert details["reason"] == "Ticketing needs it" and details["fields"] == ["date_of_birth", "documents"]
    all_events = str(connection.execute(text("SELECT details FROM saved_person_events")).scalars().all())
    assert "1234567" not in all_events and "1990-01-31" not in all_events


# ---------- holds with saved people ----------

TICKET_REF = "TICKET-REF-SECRET"


def hold_client():
    client = MagicMock()
    from unittest.mock import AsyncMock
    client.search = AsyncMock(return_value=search_data())
    client.booking = AsyncMock(return_value={"booking_status": 1, "pnrn": "63B55C", "time_limit": "2026-11-20 13:18 UTC", "pax_count": 1, "ticket_ref": TICKET_REF})
    client.fare_info = AsyncMock(return_value={"ADT": {"TOTAL": "457.70"}})
    client.pnr_info = AsyncMock(return_value=pnr_info(total=457.70, tickets=(None,)))
    client.profile = AsyncMock(return_value={"office": {"currency": "PKR"}})
    return client


def quote():
    request = FlightSearchRequest(origin="KHI", destination="MUX", departure_date="2026-11-27", adults=1)
    ref = live_flights.FareRef("outbound", "KHIMUX", "MM112@2026-11-27T23:00", "EB", "OLD-HASH", None)
    live_flights._quotes.clear()
    live_flights._store_quote(live_flights.Quote("q" * 20, ACTOR, request, "OLD-NLD", [ref], "PKR", 45_770, 46_700, [], live_flights._now() + 600))


CONTACT = {"first_name": "Test", "last_name": "Customer", "email": "ops@example.invalid", "phone_code": "92", "phone": "3001234567"}
ADULT = {"type": "ADT", "title": "MR", "first_name": "Test", "last_name": "Passenger", "date_of_birth": "1990-01-31", "id_type": "PSPT",
         "document_number": "AB1234567", "document_country": "PAK", "document_expiry": "2031-01-01", "nationality": "PAK"}


def hold(passengers, contact=CONTACT, client=None):
    from app.schemas.live_flights import HoldRequest
    quote()
    request = HoldRequest(quote_id="q" * 20, confirmed_total={"amount_minor": 46_700, "currency": "PKR"}, contact=contact, passengers=passengers)
    with patch.object(settings, "southair_booking_enabled", True), patch.object(settings, "flight_markup_percent", Decimal("2")), \
         patch.object(settings, "flight_markup_fixed_minor", 0):
        return asyncio.run(flight_holds.hold(request, ADMIN, client_factory=lambda: client or hold_client()))


def test_hold_with_traveler_id_copies_the_saved_identity_and_links_it(db):
    connection, _ = db
    saved, _ = person(document=passport(), nationality="PAK")
    client = hold_client()
    view = hold([{"type": "ADT", "traveler_id": saved.id}], client=client)
    sent = client.booking.await_args.kwargs["passengers"][0]
    assert (sent["first_name"], sent["date_of_birth"], sent["passport_no"], sent["passport_expiry"]) == ("Test", "1990-01-31", "ab1234567", "2031-01-01")
    row = connection.execute(text("SELECT person_id, document_id, document_number, date_of_birth FROM booking_travelers")).one()
    assert (row.person_id, row.document_id, row.document_number, row.date_of_birth) == (saved.id, saved.documents[0].id, "ab1234567", date(1990, 1, 31))
    assert [(o.role, o.status) for o in view.saved_people] == [("customer", "saved"), ("traveler", "linked")]


def test_hold_with_traveler_id_reruns_the_checks(db):
    expired, _ = person(document=passport(expiry=date(2026, 11, 1)))
    with pytest.raises(flight_holds.HoldError) as error:
        hold([{"type": "ADT", "traveler_id": expired.id}])
    assert error.value.status == 422 and "expires before the last travel date" in error.value.message
    child, _ = person(first="Little", dob=date(2020, 1, 1), document=passport("CD7654321"))
    with pytest.raises(flight_holds.HoldError) as error:
        hold([{"type": "ADT", "traveler_id": child.id}])
    assert error.value.status == 422 and "doesn't match the passenger type" in error.value.message


def test_identity_values_together_with_traveler_id_are_refused(db):
    saved, _ = person(document=passport())
    with pytest.raises(flight_holds.HoldError) as error:
        hold([{"type": "ADT", "traveler_id": saved.id, "date_of_birth": "1990-01-31", "document_number": "AB1234567"}])
    assert error.value.code == "identity_with_traveler_id"
    assert [path for path, _ in error.value.field_errors] == [["passengers", 0, "date_of_birth"], ["passengers", 0, "document_number"]]
    app = FastAPI()
    app.include_router(live_router, prefix="/v1")
    app.dependency_overrides[require_back_office] = lambda: ADMIN
    quote()
    with patch.object(settings, "southair_booking_enabled", True):
        response = TestClient(app).post("/v1/live-ticketing/flights/bookings", json={
            "quote_id": "q" * 20, "confirmed_total": {"amount_minor": 46_700, "currency": "PKR"}, "contact": CONTACT,
            "passengers": [{"type": "ADT", "traveler_id": saved.id, "date_of_birth": "1990-01-31"}]})
    assert response.status_code == 422 and response.json()["code"] == "identity_with_traveler_id"
    assert response.json()["detail"][0]["loc"] == ["body", "passengers", 0, "date_of_birth"]


def test_a_hold_saves_a_new_customer_and_traveler_and_links_the_booking_copies(db):
    connection, _ = db
    view = hold([ADULT])
    assert [(o.role, o.status) for o in view.saved_people] == [("customer", "saved"), ("traveler", "saved")]
    assert connection.scalar(text("SELECT count(*) FROM saved_people")) == 2
    assert connection.scalar(text("SELECT count(*) FROM booking_travelers WHERE person_id IS NOT NULL AND document_id IS NOT NULL")) == 1
    assert connection.scalar(text("SELECT count(*) FROM booking_customers WHERE person_id IS NOT NULL")) == 1
    again = hold([ADULT], client=hold_client())                                  # returning customer and traveler: linked, not duplicated
    assert [(o.role, o.status) for o in again.saved_people] == [("customer", "linked"), ("traveler", "linked")]
    assert connection.scalar(text("SELECT count(*) FROM saved_people")) == 2


def test_customer_who_is_also_the_traveler_is_one_person(db):
    connection, _ = db
    view = hold([{**ADULT, "first_name": "Test", "last_name": "Customer"}])
    customer, traveler = view.saved_people
    assert customer.person_id == traveler.person_id and connection.scalar(text("SELECT count(*) FROM saved_people")) == 1


def test_the_hold_succeeds_when_saving_needs_a_choice(db):
    connection, _ = db
    existing, _ = person(document=passport("CD7654321"))                         # same name and birth date, other passport
    view = hold([ADULT])
    assert view.pnr == "63B55C"
    traveler = view.saved_people[1]
    assert (traveler.status, traveler.code, traveler.possible_duplicate.id) == ("needs_choice", "possible_duplicate", existing.id)
    assert "CD7654321" not in traveler.model_dump_json()
    assert connection.scalar(text("SELECT person_id FROM booking_travelers")) is None


def test_a_saved_customer_is_loaded_and_a_changed_phone_updates_it(db):
    connection, _ = db
    customer, _ = person(first="Test", last="Customer", dob=None, **{k: CONTACT[k] for k in ("email", "phone_code", "phone")})
    view = hold([ADULT], contact={"customer_id": customer.id, "phone": "3009998887"})
    assert view.saved_people[0].status == "updated"
    assert connection.scalar(text("SELECT phone FROM booking_customers")) == "3009998887"
    assert connection.scalar(text("SELECT phone FROM saved_people WHERE id = :p"), {"p": customer.id}) == "3009998887"


def test_saving_failure_never_fails_the_hold(db):
    with patch.object(store, "save_from_hold", side_effect=RuntimeError("boom")):
        view = hold([ADULT])
    assert view.pnr == "63B55C" and view.saved_people[0].status == "not_saved"


def test_editing_a_saved_person_never_changes_past_bookings(db):
    connection, _ = db
    view = hold([ADULT])
    traveler_id = view.saved_people[1].person_id
    api().patch(f"/v1/admin/customers/{traveler_id}", json={"first_name": "Renamed", "date_of_birth": "1991-02-02"})
    row = connection.execute(text("SELECT first_name, date_of_birth FROM booking_travelers")).one()
    assert (row.first_name, row.date_of_birth) == ("Test", date(1990, 1, 31))


# ---------- backfill ----------

def test_backfill_creates_one_customer_per_email_and_owner_and_is_idempotent(db):
    connection, metadata = db
    first = save(connection, metadata)
    second = save(connection, metadata)                                           # same email, same owner
    connection.execute(text("UPDATE booking_customers SET person_id = NULL"))
    connection.execute(text("DELETE FROM saved_people"))
    counts = store.backfill(connection)
    assert counts == {"created": 1, "linked_booking_customers": 2}
    assert store.backfill(connection) == {"created": 0, "linked_booking_customers": 2}
    assert connection.scalar(text("SELECT count(*) FROM saved_person_documents")) == 0  # no documents backfilled
    assert first != second
