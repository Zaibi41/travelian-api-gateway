"""Admin announcements (migration 0033, travelian#92): short messages a Super Admin posts for the admin top bar.

Every back-office role reads the live ones; only Super Admin posts, ends and deletes. Each of those three actions writes
one row to admin_announcement_events (who, role, when). Deleting hides an announcement everywhere but keeps the row.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies

MIN_LENGTH = 2
MAX_LENGTH = 240

#: Statuses the Super Admin list shows. "scheduled": its start time is still ahead; "ended": ended by hand or its end
#: time has passed.
STATUSES = ("active", "scheduled", "ended")


class AnnouncementError(Exception):
    """A refused change. `status` is the HTTP status the routes answer with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Announcement:
    id: str
    message: str
    created_at: datetime
    created_by_user_id: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    active: bool
    ended_at: datetime | None
    status: str


_HTML_LIKE = re.compile(r"[<>]")
_BIDI_CONTROLS = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")  # can reorder text on screen


def clean_message(message: str | None) -> str:
    """Plain text on one line: spaces and line breaks collapse to single spaces; < and > (HTML) and control characters
    are refused rather than silently changed, so what staff see is exactly what was typed."""
    value = " ".join((message or "").split())
    if any(unicodedata.category(ch) == "Cc" or ch in _BIDI_CONTROLS for ch in value):
        raise AnnouncementError("Use plain text only.")
    if _HTML_LIKE.search(value):
        raise AnnouncementError("Use plain text only: < and > are not allowed.")
    if not MIN_LENGTH <= len(value) <= MAX_LENGTH:
        raise AnnouncementError(f"The announcement must be {MIN_LENGTH} to {MAX_LENGTH} characters.")
    return value


def check_window(starts_at: datetime | None, ends_at: datetime | None, now: datetime) -> None:
    for value in (starts_at, ends_at):
        if value is not None and value.tzinfo is None:
            raise AnnouncementError("Start and end times need a time zone.")
    if ends_at is not None and ends_at <= now:
        raise AnnouncementError("The end time must be in the future.")
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise AnnouncementError("The end time must be after the start time.")


def status_of(active: bool, starts_at: datetime | None, ends_at: datetime | None, now: datetime) -> str:
    if not active or (ends_at is not None and ends_at <= now):
        return "ended"
    if starts_at is not None and starts_at > now:
        return "scheduled"
    return "active"


_COLUMNS = "id, message, created_at, created_by_user_id, starts_at, ends_at, active, ended_at"


def _row(row, now: datetime) -> Announcement:
    return Announcement(**row, status=status_of(row["active"], row["starts_at"], row["ends_at"], now))


def _now(connection) -> datetime:
    return connection.execute(text("SELECT now()")).scalar_one()


def _event(connection, announcement_id: str, action: str, actor_user_id: str | None, actor_role: str | None) -> None:
    connection.execute(text("INSERT INTO admin_announcement_events (announcement_id, action, actor_user_id, actor_role)"
                            " VALUES (:id, :action, :user, :role)"),
                       {"id": announcement_id, "action": action, "user": actor_user_id, "role": actor_role})


def list_live() -> list[Announcement]:
    """What the top bar shows: active, not deleted, started (or no start) and not yet ended. Newest first."""
    with agencies.application_engine().begin() as connection:
        now = _now(connection)
        rows = connection.execute(text(
            f"SELECT {_COLUMNS} FROM admin_announcements WHERE active AND deleted_at IS NULL"
            " AND (starts_at IS NULL OR starts_at <= :now) AND (ends_at IS NULL OR ends_at > :now)"
            " ORDER BY created_at DESC, id"), {"now": now}).mappings().all()
    return [_row(row, now) for row in rows]


def list_all() -> list[Announcement]:
    """The Super Admin list: every announcement not deleted (active, scheduled, ended). Newest first."""
    with agencies.application_engine().begin() as connection:
        now = _now(connection)
        rows = connection.execute(text(f"SELECT {_COLUMNS} FROM admin_announcements WHERE deleted_at IS NULL"
                                       " ORDER BY created_at DESC, id")).mappings().all()
    return [_row(row, now) for row in rows]


def create(message: str, actor_user_id: str | None, actor_role: str | None, *, starts_at: datetime | None = None,
           ends_at: datetime | None = None) -> Announcement:
    value = clean_message(message)
    announcement_id = str(uuid4())
    with agencies.application_engine().begin() as connection:
        now = _now(connection)
        check_window(starts_at, ends_at, now)
        row = connection.execute(text(
            "INSERT INTO admin_announcements (id, message, created_by_user_id, starts_at, ends_at)"
            f" VALUES (:id, :message, :user, :starts, :ends) RETURNING {_COLUMNS}"),
            {"id": announcement_id, "message": value, "user": actor_user_id, "starts": starts_at, "ends": ends_at}).mappings().one()
        _event(connection, announcement_id, "create", actor_user_id, actor_role)
    return _row(row, now)


def end(announcement_id: str, actor_user_id: str | None, actor_role: str | None) -> Announcement:
    """Take it off the top bar now. It stays in the Super Admin list as ended."""
    with agencies.application_engine().begin() as connection:
        now = _now(connection)
        row = connection.execute(text(
            "UPDATE admin_announcements SET active = false, ended_at = :now, ended_by_user_id = :user"
            f" WHERE id = :id AND active AND deleted_at IS NULL RETURNING {_COLUMNS}"),
            {"id": announcement_id, "user": actor_user_id, "now": now}).mappings().one_or_none()
        if not row:
            raise AnnouncementError("No active announcement with that id.", 404)
        _event(connection, announcement_id, "end", actor_user_id, actor_role)
    return _row(row, now)


def delete(announcement_id: str, actor_user_id: str | None, actor_role: str | None) -> None:
    """Remove it from the top bar and the Super Admin list. The row and its audit events are kept."""
    with agencies.application_engine().begin() as connection:
        now = _now(connection)
        found = connection.execute(text(
            "UPDATE admin_announcements SET deleted_at = :now, deleted_by_user_id = :user,"
            " active = false, ended_at = COALESCE(ended_at, :now), ended_by_user_id = COALESCE(ended_by_user_id, :user)"
            " WHERE id = :id AND deleted_at IS NULL RETURNING id"),
            {"id": announcement_id, "user": actor_user_id, "now": now}).scalar_one_or_none()
        if not found:
            raise AnnouncementError("No announcement with that id.", 404)
        _event(connection, announcement_id, "delete", actor_user_id, actor_role)


def events(announcement_id: str) -> list[dict]:
    """The audit trail of one announcement, oldest first (tests and support)."""
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text("SELECT action, actor_user_id, actor_role, at FROM admin_announcement_events"
                                       " WHERE announcement_id = :id ORDER BY id"), {"id": announcement_id}).mappings().all()
    return [dict(row) for row in rows]

