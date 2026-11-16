"""Saved customers and travelers (migration 0017): matching, scope, masking, duplicates, merge,
resolving a hold's possible duplicate from the booking, holds with saved people, and the backfill. Synthetic people only."""

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

def test_search_and_detail_carry_the_full_values(db):
    saved, _ = person(document=passport(), email="ops@example.invalid", phone_code="92", phone="3001234567")
    store.add_document(None, ACTOR, saved.id, cnic())
    client = api()
    found = client.get("/v1/admin/customers", params={"q": "AB1234567"})
    detail = client.get(f"/v1/admin/customers/{saved.id}")
    for response in (found, detail):
        assert response.status_code == 200 and "•" not in response.text and "_masked" not in response.text
    result = found.json()["results"][0]
    assert (result["date_of_birth"], result["phone"], result["email"]) == ("1990-01-31", "+92 3001234567", "ops@example.invalid")
    assert {d["document_number"] for d in result["documents"]} == {PASSPORT, CNIC}
    assert {d.get("document_expiry") for d in result["documents"]} == {"2031-01-01", None}
    assert {d.get("document_validity") for d in result["documents"]} == {"valid", None}
    assert detail.json()["date_of_birth"] == "1990-01-31" and "can_reveal_identity" not in detail.json()


def test_search_needs_three_characters_and_returns_at_most_twenty(db):
    for index in range(22):
        person(first="Zain", last=f"Synthetic{chr(65 + index)}", dob=date(1990, 1, 1) + timedelta(days=index))
    client = api()
    assert client.get("/v1/admin/customers", params={"q": "za"}).status_code == 422
    found = client.get("/v1/admin/customers", params={"q": "zai"}).json()
    assert len(found["results"]) == 20 and (found["can_manage"], found["can_merge"]) == (True, True)
    staff = api(STAFF).get("/v1/admin/customers", params={"q": "zai"}).json()
    assert (staff["can_manage"], staff["can_merge"]) == (False, False)          # the flags come from the caller's permissions


def test_without_q_the_register_lists_the_most_recent_people(db):
    """The Customers page's first view: latest booking first, then the most recently saved; merged
    people and other owners' people left out; masked values only; at most 50."""
    connection, metadata = db
    old, _ = person(first="Old", dob=date(1980, 1, 1))
    newer, _ = person(first="Newer", dob=date(1981, 1, 1), phone_code="92", phone="3001234567")
    booked, _ = person(first="Booked", dob=date(1982, 1, 1))
    merged, _ = person(first="Merged", dob=date(1983, 1, 1))
    agency, _ = person(owner="ag-1", first="Agency", dob=date(1984, 1, 1))
    connection.execute(text("UPDATE saved_people SET updated_at = now() - interval '2 days' WHERE id = :p"), {"p": old.id})
    connection.execute(text("UPDATE saved_people SET merged_into_id = :t WHERE id = :p"), {"p": merged.id, "t": newer.id})
    reference = save(connection, metadata, pnr="ABC777")
    connection.execute(text("UPDATE booking_customers SET person_id = :p WHERE booking_id = (SELECT id FROM bookings WHERE reference = :r)"),
                       {"p": booked.id, "r": reference})

    names = [p.first_name for p in store.recent(None, 50)]
    assert names[0] == "Booked" and names.index("Newer") < names.index("Old")
    assert "Merged" not in names and "Agency" not in names
    assert [p.first_name for p in store.recent("ag-1", 50)] == ["Agency"]

    client = api()
    listed = client.get("/v1/admin/customers", params={"limit": 50}).json()
    assert [r["first_name"] for r in listed["results"]] == names and listed["can_manage"] is True
    assert any(r.get("phone") == "+92 3001234567" for r in listed["results"])
    assert "match" not in listed["results"][0]
    assert client.get("/v1/admin/customers", params={"limit": 51}).status_code == 422
    assert len(client.get("/v1/admin/customers", params={"limit": 2}).json()["results"]) == 2


# ---------- duplicates and conflicts ----------

def test_same_name_and_birth_date_with_another_document_is_a_possible_duplicate(db):
    first, _ = person(document=passport("AB1234567"))
    client = api()
    body = {"title": "MR", "first_name": "Test", "last_name": "Passenger", "date_of_birth": "1990-01-31",
            "document": {"id_type": "PSPT", "document_country": "PAK", "document_number": "CD7654321", "document_expiry": "2033-01-01"}}
    response = client.post("/v1/admin/customers", json=body)
    assert response.status_code == 409 and response.json()["code"] == "possible_duplicate"
    assert response.json()["possible_duplicate"]["id"] == first.id
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


# ---------- merge ----------

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


# ---------- resolving a hold's needs_choice from the booking's own copy ----------

def needs_choice_booking(db):
    """A hold whose traveler is a possible duplicate of a saved person (same name and birth date, other passport)."""
    existing, _ = person(document=passport("CD7654321"))
    view = hold([ADULT])
    assert view.saved_people[1].status == "needs_choice"
    return existing, view.booking_id


def resolve_url(reference, role="traveler", position=0):
    return f"/v1/admin/bookings/{reference}/saved-people/{role}/{position}/resolve"


def booking_api(viewer=ADMIN):
    app = FastAPI()
    app.include_router(routes.booking_router, prefix="/v1")
    app.dependency_overrides[require_back_office] = lambda: viewer
    return TestClient(app)


def test_resolve_add_to_existing_uses_the_booking_copy_and_links_it(db):
    connection, _ = db
    existing, reference = needs_choice_booking(db)
    response = booking_api().post(resolve_url(reference), json={"choice": "add_to_existing", "person_id": existing.id})
    assert response.status_code == 200 and response.json()["outcome"] == "added_to_existing"
    body = response.json()["person"]
    assert body["id"] == existing.id and len(body["documents"]) == 2
    row = connection.execute(text("SELECT person_id, document_id FROM booking_travelers")).one()
    added = connection.scalar(text("SELECT id FROM saved_person_documents WHERE document_number_normalized = 'AB1234567'"))
    assert (row.person_id, row.document_id) == (existing.id, added)
    events = connection.execute(text("SELECT action, details FROM saved_person_events WHERE person_id = :p AND details->>'source' = 'booking'"),
                                {"p": existing.id}).all()
    assert {e.action for e in events} == {"duplicate_choice", "linked_to_booking"}
    assert "AB1234567" not in str([e.details for e in events]) and "1990-01-31" not in str([e.details for e in events])


def test_resolve_keep_separate_creates_a_new_person(db):
    connection, _ = db
    existing, reference = needs_choice_booking(db)
    response = booking_api().post(resolve_url(reference), json={"choice": "keep_separate", "person_id": existing.id})
    assert response.status_code == 201 and response.json()["outcome"] == "created"
    new_id = response.json()["person"]["id"]
    assert new_id != existing.id and connection.scalar(text("SELECT person_id FROM booking_travelers")) == new_id
    assert connection.scalar(text("SELECT details->>'other_person_id' FROM saved_person_events WHERE person_id = :p AND action = 'duplicate_choice'"),
                             {"p": new_id}) == existing.id


def test_resolve_refuses_a_row_that_is_already_linked(db):
    existing, reference = needs_choice_booking(db)
    client = booking_api()
    assert client.post(resolve_url(reference), json={"choice": "keep_separate"}).status_code == 201
    again = client.post(resolve_url(reference), json={"choice": "add_to_existing", "person_id": existing.id})
    assert again.status_code == 409 and again.json()["code"] == "already_linked"
    customer = client.post(resolve_url(reference, "customer", 0), json={"choice": "keep_separate"})   # the hold linked the customer
    assert customer.status_code == 409 and customer.json()["code"] == "already_linked"


def test_resolve_needs_manage_people(db):
    connection, _ = db
    existing, reference = needs_choice_booking(db)
    response = booking_api(STAFF).post(resolve_url(reference), json={"choice": "add_to_existing", "person_id": existing.id})
    assert response.status_code == 403 and response.json()["code"] == "not_allowed"
    assert connection.scalar(text("SELECT person_id FROM booking_travelers")) is None
    granted = BackOfficeUser(ACTOR, "staff", NONE, frozenset({"manage_people"}))
    assert booking_api(granted).post(resolve_url(reference), json={"choice": "add_to_existing", "person_id": existing.id}).status_code == 200


def test_resolve_refuses_a_booking_the_caller_cannot_see(db):
    connection, _ = db
    existing, reference = needs_choice_booking(db)
    connection.execute(text("UPDATE bookings SET agency_id = 'ag-1'"))         # another owner's booking
    client = booking_api()
    response = client.post(resolve_url(reference), json={"choice": "add_to_existing", "person_id": existing.id})
    assert response.status_code == 404 and response.json()["code"] == "booking_not_found"
    assert client.post(resolve_url("TRV-NOSUCH"), json={"choice": "keep_separate"}).json()["code"] == "booking_not_found"
    assert connection.scalar(text("SELECT person_id FROM booking_travelers")) is None


def test_resolve_applies_the_create_rules(db):
    connection, _ = db
    existing, reference = needs_choice_booking(db)
    client = booking_api()
    assert client.post(resolve_url(reference), json={"choice": "add_to_existing"}).json()["code"] == "person_required"
    assert client.post(resolve_url(reference, position=5), json={"choice": "keep_separate"}).json()["code"] == "booking_person_not_found"
    other, _ = person(first="Someone", last="Else", dob=date(1970, 3, 3), document=passport("ZZ1111111"))
    connection.execute(text("UPDATE booking_travelers SET document_number = 'ZZ1111111'"))
    conflict = client.post(resolve_url(reference), json={"choice": "keep_separate"})
    assert conflict.status_code == 409 and conflict.json()["code"] == "document_belongs_to_another_person"
    connection.execute(text("UPDATE booking_travelers SET document_number = 'AB1234567', document_expiry = NULL"))
    invalid = client.post(resolve_url(reference), json={"choice": "keep_separate"})       # a passport without expiry fails the checks
    assert invalid.status_code == 422 and invalid.json()["code"] == "booking_values_invalid"
    assert connection.scalar(text("SELECT person_id FROM booking_travelers")) is None and other.id != existing.id


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


# ---------- the shared customer card (both hold forms): what is typed is what is saved ----------

CARD = {"full_name": "Test Customer", "phone": "+92 300 1234567", "email": "ops@example.invalid", "id_type": "PSPT", "document_number": "AB1234567",
        "save_for_next_time": True}
CUSTOMER_TRAVELS = {**ADULT, "first_name": "Test", "last_name": "Customer"}  # "The customer is also Adult 1"


def card_hold(passengers, card=CARD, client=None):
    from app.schemas.live_flights import HoldRequest
    quote()
    request = HoldRequest(quote_id="q" * 20, confirmed_total={"amount_minor": 46_700, "currency": "PKR"}, customer=card, passengers=passengers)
    with patch.object(settings, "southair_booking_enabled", True), patch.object(settings, "flight_markup_percent", Decimal("2")), \
         patch.object(settings, "flight_markup_fixed_minor", 0):
        return asyncio.run(flight_holds.hold(request, ADMIN, client_factory=lambda: client or hold_client()))


def saved(person_id):
    found, _ = store.get(None, person_id)
    return found


def test_a_new_customer_from_the_card_keeps_phone_email_and_document_and_holds_again_when_picked(db):
    view = card_hold([CUSTOMER_TRAVELS])
    customer, traveler = view.saved_people
    assert customer.person_id == traveler.person_id                       # one person: the customer travels
    person = saved(customer.person_id)
    assert (person.phone_code, person.phone, person.email) == ("92", "3001234567", "ops@example.invalid")
    assert [(d.id_type, d.document_country, d.document_number) for d in person.documents] == [("PSPT", "PAK", "AB1234567")]
    detail = api().get(f"/v1/admin/customers/{person.id}").json()
    assert (detail["phone"], detail["email"]) == ("+92 3001234567", "ops@example.invalid")   # Find saved customer shows them in full
    # Second booking: pick the saved customer; the hold needs nothing typed.
    again = card_hold([{"type": "ADT", "traveler_id": person.id}], card={"customer_id": person.id, "save_for_next_time": True}, client=hold_client())
    assert again.pnr == "63B55C" and again.saved_people[0].status == "linked"


def test_a_phone_shared_with_someone_else_no_longer_drops_the_customer(db):
    # The reported bug: another saved person (other name) already had this phone, so the customer save stopped at
    # "possible duplicate" and only the traveler, without phone or email, was saved.
    person(first="Other", last="Relative", dob=None, phone_code="92", phone="3001234567", email="other@example.invalid")
    connection, _ = db
    view = card_hold([CUSTOMER_TRAVELS])
    customer, traveler = view.saved_people
    assert customer.status == "linked" and customer.person_id == traveler.person_id
    person_saved = saved(customer.person_id)
    assert (person_saved.phone_code, person_saved.phone, person_saved.email) == ("92", "3001234567", "ops@example.invalid")
    assert connection.scalar(text("SELECT person_id FROM booking_customers")) == customer.person_id


def test_a_picked_customer_takes_newer_values_and_an_empty_one_never_blanks_a_stored_value(db):
    customer, _ = person(first="Test", last="Customer", dob=None, phone_code="92", phone="3001234567", email="old@example.invalid")
    view = card_hold([ADULT], card={"customer_id": customer.id, "email": "new@example.invalid", "save_for_next_time": True})
    assert view.saved_people[0].status == "updated"
    after = saved(customer.id)
    assert (after.email, after.phone_code, after.phone) == ("new@example.invalid", "92", "3001234567")   # phone kept, not blanked
    card_hold([ADULT], card={"customer_id": customer.id, "save_for_next_time": True}, client=hold_client())
    assert saved(customer.id).email == "new@example.invalid"


def test_a_picked_customer_without_a_document_gets_the_one_typed_on_the_card(db):
    customer, _ = person(first="Test", last="Customer", dob=None, phone_code="92", phone="3001234567", email="ops@example.invalid")
    card_hold([ADULT], card={"customer_id": customer.id, "id_type": "NATI", "document_number": "35202-1234567-1", "save_for_next_time": True})
    assert [(d.id_type, d.document_country, d.document_number) for d in saved(customer.id).documents] == [("NATI", "PAK", "3520212345671")]  # the card stores it without spaces or dashes


def test_traveler_fields_round_trip_and_a_known_passport_fills_what_the_record_lacks(db):
    view = card_hold([{**ADULT, "title": "MRS", "first_name": "Sana", "last_name": "Raza", "nationality": "GBR", "document_country": "GBR",
                       "date_of_birth": "1992-03-04", "document_expiry": "2032-05-06"}])
    traveler = saved(view.saved_people[1].person_id)
    assert (traveler.title, traveler.first_name, traveler.last_name, traveler.date_of_birth, traveler.nationality) == ("MRS", "Sana", "Raza", date(1992, 3, 4), "GBR")
    assert [(d.id_type, d.document_country, d.document_number, d.document_expiry) for d in traveler.documents] == [("PSPT", "GBR", "AB1234567", date(2032, 5, 6))]
    bare, _ = person(first="Ali", last="Khan", dob=None, document=passport("ZX9876543"))      # no birth date, nationality or title
    card_hold([{**ADULT, "first_name": "Ali", "last_name": "Khan", "document_number": "ZX9876543", "nationality": "PAK", "date_of_birth": "1985-07-08"}],
              client=hold_client())
    filled = saved(bare.id)
    assert (filled.date_of_birth, filled.nationality) == (date(1985, 7, 8), "PAK")


def test_repair_script_counts_then_fills_only_empty_values(db, capsys):
    from app.cli import repair_saved_contacts
    connection, _ = db
    # A booking as the bug left it: the traveler saved without contact details, the customer row unlinked.
    view = card_hold([CUSTOMER_TRAVELS])
    person_id = view.saved_people[1].person_id
    connection.execute(text("UPDATE saved_people SET phone_code = NULL, phone = NULL, phone_normalized = NULL, email = NULL, email_normalized = NULL"))
    connection.execute(text("UPDATE booking_customers SET person_id = NULL"))
    assert repair_saved_contacts.main([]) == 0
    assert "Booking customers to link to their saved traveler: 1. Saved people that would gain an email or phone: 1. Nothing changed." in capsys.readouterr().out
    assert saved(person_id).email is None
    assert repair_saved_contacts.main(["--apply"]) == 0
    after = saved(person_id)
    assert (after.phone_code, after.phone, after.email) == ("92", "3001234567", "ops@example.invalid")
    assert connection.scalar(text("SELECT person_id FROM booking_customers")) == person_id
    repair_saved_contacts.main([])
    assert "Saved people that would gain an email or phone: 0" in capsys.readouterr().out


# ---------- live search on the hold form (customers and travelers), from 2 characters, at most 8 ----------

def typeahead(path, q, viewer=ADMIN):
    from app.api.routes.saved_people import travelers_router
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    app.include_router(travelers_router, prefix="/v1")
    app.dependency_overrides[require_back_office] = lambda: viewer
    return TestClient(app).get(f"/v1/admin/{path}/search", params={"q": q})


def names(response):
    return [f"{p['first_name']} {p['last_name']}" for p in response.json()["results"]]


def test_customer_search_matches_partial_names_phone_email_and_document_prefix(db):
    person(first="Usman", last="Tariq", dob=None, phone_code="92", phone="3125551234", email="usman.t@example.invalid", document=passport("AB5554967"))
    person(first="Sana", last="Raza", dob=None, phone_code="92", phone="3009998887", email="sana@example.invalid")
    for q in ("usm", "USMAN", "tariq", "man tar", "Usman Tariq"):
        assert names(typeahead("customers", q)) == ["Usman Tariq"], q
    assert names(typeahead("customers", "5551")) == ["Usman Tariq"]                       # phone contains
    assert names(typeahead("customers", "sana@exa")) == ["Sana Raza"]                     # email contains
    assert names(typeahead("customers", "ab555")) == ["Usman Tariq"]                      # document number prefix
    result = typeahead("customers", "usm").json()["results"][0]
    assert result["documents"][0]["document_number"] == "AB5554967"
    assert (result["phone"], result["email"]) == ("+92 3125551234", "usman.t@example.invalid")
    assert typeahead("customers", "u").status_code == 422                                  # 2 characters at least
    assert names(typeahead("customers", "zzz")) == []


def test_traveler_search_matches_name_and_document_but_not_contact(db):
    person(first="Usman", last="Tariq", dob=None, phone_code="92", phone="3125551234", document=passport("AB5554967"))
    assert names(typeahead("travelers", "tar")) == ["Usman Tariq"]
    assert names(typeahead("travelers", "AB55")) == ["Usman Tariq"]
    assert names(typeahead("travelers", "5551")) == []


def test_search_returns_at_most_eight_names_starting_with_the_text_first_and_treats_wildcards_literally(db):
    for n in range(10):
        person(first=f"Ali{n}", last="Khan", dob=None)
    person(first="Malik", last="Ahmed", dob=None)
    found = names(typeahead("customers", "ali"))
    assert len(found) == 8 and all(name.startswith("Ali") for name in found)
    assert names(typeahead("customers", "%_")) == []


def test_search_is_back_office_only(db):
    from app.core.field_visibility import require_back_office as dependency
    from fastapi import HTTPException

    def refuse():
        raise HTTPException(403, "Not back office.")
    from app.api.routes.saved_people import travelers_router
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    app.include_router(travelers_router, prefix="/v1")
    app.dependency_overrides[dependency] = refuse
    client = TestClient(app)
    assert client.get("/v1/admin/customers/search", params={"q": "usm"}).status_code == 403
    assert client.get("/v1/admin/travelers/search", params={"q": "usm"}).status_code == 403
