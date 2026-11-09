"""A booking's customer, travelers (masked) and audit trail; the logged identity reveal.

Reads never decrypt: they return the masked date of birth and document number stored with
each traveler. reveal() decrypts one traveler's identity fields and writes an
"identity_revealed" event (who, which traveler, which fields, reason) in the same
transaction, so there is no reveal without its log entry. Values are never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from app.core import pii
from app.db import agencies
from app.db.supplier_bookings import _event, identity_context

#: Audit detail keys that may be shown (never names, documents, dates of birth or contact details).
AUDIT_DETAIL_KEYS = ("label", "pnr", "time_limit", "fare_info", "pax_count", "travelers", "traveler_position", "fields", "reason")


@dataclass(frozen=True)
class Customer:
    first_name: str
    last_name: str
    phone_code: str
    phone: str
    email: str


@dataclass(frozen=True)
class Traveler:
    position: int
    passenger_type: str
    title: str
    first_name: str
    last_name: str
    date_of_birth_masked: str
    id_type: str
    document_number_masked: str | None
    document_country: str | None
    nationality: str | None
    document_expiry: date | None
    infant_of_position: int | None


@dataclass(frozen=True)
class AuditEvent:
    action: str
    actor_user_id: str | None
    created_at: datetime
    details: dict[str, Any]


@dataclass(frozen=True)
class People:
    customer: Customer | None
    travelers: list[Traveler]
    events: list[AuditEvent]


def _booking_id(connection, reference: str) -> str | None:
    return connection.scalar(text("SELECT id FROM bookings WHERE reference = :r"), {"r": reference})


def read(reference: str) -> People | None:
    with agencies.application_engine().begin() as connection:
        booking_id = _booking_id(connection, reference)
        if booking_id is None:
            return None
        row = connection.execute(
            text("SELECT first_name, last_name, phone_code, phone, email FROM booking_customers WHERE booking_id = :b"),
            {"b": booking_id},
        ).mappings().one_or_none()
        travelers = connection.execute(
            text("SELECT position, passenger_type, title, first_name, last_name, date_of_birth_masked, id_type,"
                 " document_number_masked, document_country, nationality, document_expiry, infant_of_position"
                 " FROM booking_travelers WHERE booking_id = :b ORDER BY position"),
            {"b": booking_id},
        ).mappings().all()
        events = connection.execute(
            text("SELECT action, actor_user_id, created_at, details FROM booking_events WHERE booking_id = :b ORDER BY created_at, id"),
            {"b": booking_id},
        ).mappings().all()
    return People(
        customer=Customer(**row) if row else None,
        travelers=[Traveler(**t) for t in travelers],
        events=[AuditEvent(e["action"], e["actor_user_id"], e["created_at"],
                           {k: v for k, v in (e["details"] or {}).items() if k in AUDIT_DETAIL_KEYS}) for e in events],
    )


class NotFound(Exception):
    pass


def reveal(reference: str, position: int, actor_user_id: str, reason: str) -> dict[str, str | None]:
    """Decrypt one traveler's date of birth and document number and log the reveal (one transaction)."""
    keys = pii.load_keys()
    with agencies.application_engine().begin() as connection:
        booking_id = _booking_id(connection, reference)
        row = connection.execute(
            text("SELECT date_of_birth_encrypted, document_number_encrypted FROM booking_travelers"
                 " WHERE booking_id = :b AND position = :p"),
            {"b": booking_id, "p": position},
        ).mappings().one_or_none() if booking_id else None
        if row is None:
            raise NotFound()
        values = {
            "date_of_birth": pii.decrypt(row["date_of_birth_encrypted"], identity_context(booking_id, position, "date_of_birth"), keys),
            "document_number": pii.decrypt(row["document_number_encrypted"], identity_context(booking_id, position, "document_number"), keys)
            if row["document_number_encrypted"] else None,
        }
        _event(connection, booking_id, actor_user_id, "identity_revealed",
               {"traveler_position": position, "fields": [k for k, v in values.items() if v is not None], "reason": reason})
    return values
