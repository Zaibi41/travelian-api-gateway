"""gateway#89: staff notes on a booking (migration 0039): add and list newest first, logged without the text, back office only."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.routes import admin_bookings as routes
from app.core.field_visibility import NONE, BackOfficeUser, require_back_office
from app.db import booking_notes, booking_people
from tests.database.test_booking_people import ACTOR, save

STAFF = BackOfficeUser(ACTOR, "staff", NONE)


def api(viewer=STAFF):
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    if viewer is None:
        def deny():
            raise HTTPException(403, "Back office only.")
        app.dependency_overrides[require_back_office] = deny
    else:
        app.dependency_overrides[require_back_office] = lambda: viewer
    return TestClient(app)


def test_add_and_list_newest_first_and_the_audit_trail_has_no_text(database):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    first = api().post(f"/v1/admin/bookings/{reference}/notes", json={"body": "  Customer will pay by Friday.  "})
    assert first.status_code == 201 and first.json()["body"] == "Customer will pay by Friday."
    assert first.json()["created_by_user_id"] == ACTOR
    api().post(f"/v1/admin/bookings/{reference}/notes", json={"body": "Called the airline about the name."})
    notes = api().get(f"/v1/admin/bookings/{reference}/notes").json()["notes"]
    assert [n["body"] for n in notes] == ["Called the airline about the name.", "Customer will pay by Friday."]
    events = [e for e in booking_people.read(reference).events if e.action == "note_added"]
    assert len(events) == 2 and all(e.details == {} for e in events)
    stored = connection.execute(text("SELECT string_agg(details::text, ' ') FROM booking_events WHERE action = 'note_added'")).scalar()
    assert "Friday" not in stored


@pytest.mark.parametrize("body", ["", "   ", "x" * 1001])
def test_empty_or_too_long_is_refused_and_nothing_is_saved(database, body):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    assert api().post(f"/v1/admin/bookings/{reference}/notes", json={"body": body}).status_code == 422
    assert connection.scalar(text("SELECT count(*) FROM booking_notes")) == 0
    with pytest.raises(booking_notes.NoteRefused):
        booking_notes.add_note(reference, body, ACTOR)


def test_unknown_booking_and_non_back_office(database):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    assert api().get("/v1/admin/bookings/TRV-NOPE00/notes").status_code == 404
    assert api().post("/v1/admin/bookings/TRV-NOPE00/notes", json={"body": "x"}).status_code == 404
    assert api(None).get(f"/v1/admin/bookings/{reference}/notes").status_code == 403
    assert api(None).post(f"/v1/admin/bookings/{reference}/notes", json={"body": "x"}).status_code == 403
