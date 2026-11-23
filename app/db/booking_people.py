"""A booking's customer, travelers and audit trail. Identity values (date of birth, document number and expiry) are
returned in full to back-office readers, as stored (docs/pii-storage.md). Values are never logged."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from app.db import agencies, booking_payments, identity
from app.db.supplier_bookings import _event

#: Audit detail keys that may be shown (never names, documents, dates of birth or contact details).
AUDIT_DETAIL_KEYS = ("label", "pnr", "time_limit", "fare_info", "pax_count", "travelers", "traveler_position", "fields", "reason",
                     "amount_minor", "currency", "method", "received_into", "payer_reference", "payment_status",
                     "note", "previous_status", "reason_code", "supplier_answer", "supplier_error", "pnr_status", "tickets", "outcome",
                     "total_source", "pnr_info_error", "fare_info_error", "previous_amount_minor",
                     "terms_version", "terms_text", "acknowledged_by", "final_pnr", "previous_final_pnr")


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
    id_type: str
    document_country: str | None
    nationality: str | None
    document_expiry: date | None
    infant_of_position: int | None
    date_of_birth: date
    document_number: str | None
    ticket_number: str | None = None
    selling_minor: int | None = None  # this traveler's selling price (migration 0021)
    cost_minor: int | None = None     # this traveler's supplier cost; never sent without cost and margin access


@dataclass(frozen=True)
class AuditEvent:
    action: str
    actor_user_id: str | None
    created_at: datetime
    details: dict[str, Any]
    actor_role: str | None = None


@dataclass(frozen=True)
class People:
    customer: Customer | None
    travelers: list[Traveler]
    events: list[AuditEvent]
    payments: list[booking_payments.Payment] = field(default_factory=list)


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
            text("SELECT position, passenger_type, title, first_name, last_name, date_of_birth, date_of_birth_enc, id_type,"
                 " document_number, document_number_enc, document_country, nationality, document_expiry, infant_of_position, ticket_number, selling_minor, cost_minor"
                 " FROM booking_travelers WHERE booking_id = :b ORDER BY position"),
            {"b": booking_id},
        ).mappings().all()
        payments = booking_payments.list_for(connection, booking_id)
        events = connection.execute(
            text("SELECT e.action, e.actor_user_id, e.created_at, e.details, r.name AS actor_role FROM booking_events e"
                 " LEFT JOIN users u ON u.id = e.actor_user_id LEFT JOIN roles r ON r.id = u.role_id"
                 " WHERE e.booking_id = :b ORDER BY e.created_at, e.id"),
            {"b": booking_id},
        ).mappings().all()
    return People(
        customer=Customer(**row) if row else None,
        travelers=[Traveler(**identity.open_traveler(dict(t))) for t in travelers],
        events=[AuditEvent(e["action"], e["actor_user_id"], e["created_at"],
                           {k: v for k, v in (e["details"] or {}).items() if k in AUDIT_DETAIL_KEYS}, e["actor_role"]) for e in events],
        payments=payments,
    )


class NotFound(Exception):
    pass
