"""Super Admin corrections to a booking's travelers and customer name (travelian#225)."""

from datetime import date

import pytest
from sqlalchemy import text

from app.db import booking_corrections as corrections
from app.db import booking_people
from tests.database.test_booking_people import ACTOR, save


def test_traveler_correction_changes_only_the_given_fields_and_logs_fields_and_reason_never_values(database):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    result = corrections.correct_traveler(reference, 0, {"first_name": "Samples", "last_name": "Passenger", "document_number": "AB7654321"},
                                          "Typo in the passport name", ACTOR, today=date(2026, 11, 26))
    assert result.fields == ["document_number", "first_name"]  # last_name was the same: not a change
    assert result.sent_to_airline is True  # the hold has a PNR
    people = booking_people.read(reference)
    adult = people.travelers[0]
    assert (adult.first_name, adult.last_name, adult.document_number, adult.date_of_birth) == ("Samples", "Passenger", "AB7654321", date(1990, 1, 31))
    event = people.events[-1]
    assert event.action == "traveler_corrected" and event.actor_user_id == ACTOR
    assert event.details == {"traveler_position": 0, "fields": ["document_number", "first_name"], "reason": "Typo in the passport name"}
    stored = connection.execute(text("SELECT details::text FROM booking_events WHERE action = 'traveler_corrected'")).scalar()
    assert "AB7654321" not in stored and "Samples" not in stored


@pytest.mark.parametrize(("position", "changes", "reason", "status"), [
    (5, {"first_name": "X"}, "a good reason", 404),
    (0, {"first_name": "Sample"}, "a good reason", 409),  # same value
    (0, {}, "a good reason", 409),
    (0, {"first_name": "Other"}, "no", 422),  # reason too short
    (0, {"date_of_birth": date(2027, 1, 1)}, "a good reason", 422),
    (1, {"title": "MR"}, "a good reason", 422),  # infant
])
def test_traveler_correction_refusals_change_nothing(database, position, changes, reason, status):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    with pytest.raises(corrections.CorrectionRefused) as refused:
        corrections.correct_traveler(reference, position, changes, reason, ACTOR, today=date(2026, 11, 26))
    assert refused.value.status == status
    assert [t.first_name for t in booking_people.read(reference).travelers] == ["Sample", "Baby"]
    assert not connection.execute(text("SELECT 1 FROM booking_events WHERE action = 'traveler_corrected'")).first()


def test_unknown_booking_is_404(database):
    with pytest.raises(corrections.CorrectionRefused) as refused:
        corrections.correct_traveler("TRV-NOPE00", 0, {"first_name": "X"}, "a good reason", ACTOR)
    assert refused.value.status == 404


def test_customer_name_correction(database):
    connection, metadata, _ = database
    reference = save(connection, metadata, pnr=None)
    result = corrections.correct_customer(reference, {"first_name": " Testa ", "last_name": "Customer"}, "Customer spelled it wrong", ACTOR)
    assert (result.fields, result.sent_to_airline) == (["first_name"], False)
    people = booking_people.read(reference)
    assert (people.customer.first_name, people.customer.last_name, people.customer.email) == ("Testa", "Customer", "ops@example.invalid")
    assert people.events[-1].action == "customer_corrected"
    assert people.events[-1].details == {"fields": ["first_name"], "reason": "Customer spelled it wrong"}
    with pytest.raises(corrections.CorrectionRefused) as refused:
        corrections.correct_customer(reference, {"first_name": "Testa"}, "Customer spelled it wrong", ACTOR)
    assert refused.value.status == 409


def _api(viewer):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.api.routes import admin_bookings as routes
    from app.core.field_visibility import require_super_admin

    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")

    def gate():
        if viewer.role != "super_admin":
            raise HTTPException(403, "Super Admin only.")
        return viewer
    app.dependency_overrides[require_super_admin] = gate
    return TestClient(app)


def test_routes_are_super_admin_only_and_need_a_reason(database):
    from app.core.field_visibility import ALL, NONE, BackOfficeUser, resolve_actions

    connection, metadata, _ = database
    reference = save(connection, metadata)
    admin = BackOfficeUser(ACTOR, "super_admin", ALL, resolve_actions("super_admin", set()))
    staff = BackOfficeUser(ACTOR, "staff", NONE)
    body = {"first_name": "Samples", "reason": "Typo in the name"}
    assert _api(staff).put(f"/v1/admin/bookings/{reference}/travelers/0", json=body).status_code == 403
    assert _api(admin).put(f"/v1/admin/bookings/{reference}/travelers/0", json={"first_name": "Samples"}).status_code == 422
    answer = _api(admin).put(f"/v1/admin/bookings/{reference}/travelers/0", json=body)
    assert answer.status_code == 200, answer.text
    assert answer.json()["fields"] == ["first_name"] and answer.json()["sent_to_airline"] is True
    assert "airline" in answer.json()["message"]
    assert _api(staff).put(f"/v1/admin/bookings/{reference}/customer-name", json={"first_name": "A", "reason": "Typo in the name"}).status_code == 403
    answer = _api(admin).put(f"/v1/admin/bookings/{reference}/customer-name", json={"last_name": "Customers", "reason": "Typo in the name"})
    assert answer.status_code == 200 and answer.json()["fields"] == ["last_name"]
