"""Staff notes on a booking (gateway#89, migration 0039). Back office only; append-only. Adding a note also writes a
"note_added" booking event (who and when, never the text), so the Audit tab shows it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies
from app.db.supplier_bookings import _event

MAX_LENGTH = 1000


class NoteRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Note:
    id: str
    body: str
    created_at: datetime
    created_by_user_id: str | None
    created_by_role: str | None


_SELECT = ("SELECT n.id, n.body, n.created_at, n.created_by_user_id, r.name AS created_by_role FROM booking_notes n"
           " LEFT JOIN users u ON u.id = n.created_by_user_id LEFT JOIN roles r ON r.id = u.role_id")


def _note(row) -> Note:
    return Note(row["id"], row["body"], row["created_at"], row["created_by_user_id"], row["created_by_role"])


def _booking_id(connection, reference: str) -> str:
    booking_id = connection.scalar(text("SELECT id FROM bookings WHERE reference = :r"), {"r": reference})
    if booking_id is None:
        raise NoteRefused(404, "Booking not found.")
    return booking_id


def list_notes(reference: str) -> list[Note]:
    """Newest first."""
    with agencies.application_engine().begin() as connection:
        booking_id = _booking_id(connection, reference)
        rows = connection.execute(text(_SELECT + " WHERE n.booking_id = :b ORDER BY n.created_at DESC, n.id DESC"), {"b": booking_id}).mappings().all()
    return [_note(row) for row in rows]


def add_note(reference: str, body: str, actor_user_id: str) -> Note:
    body = (body or "").strip()
    if not body:
        raise NoteRefused(422, "Write the note first.")
    if len(body) > MAX_LENGTH:
        raise NoteRefused(422, f"A note can be up to {MAX_LENGTH} characters.")
    with agencies.application_engine().begin() as connection:
        booking_id = _booking_id(connection, reference)
        note_id = str(uuid4())
        connection.execute(text("INSERT INTO booking_notes (id, booking_id, body, created_by_user_id) VALUES (:id, :b, :body, :actor)"),
                           {"id": note_id, "b": booking_id, "body": body, "actor": actor_user_id})
        _event(connection, booking_id, actor_user_id, "note_added", {})
        row = connection.execute(text(_SELECT + " WHERE n.id = :id"), {"id": note_id}).mappings().one()
    return _note(row)
