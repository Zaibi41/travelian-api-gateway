"""Travelers of open bookings with a given name, for the double-booking check (app.services.duplicate_bookings).

Read-only. Names are compared case- and space-insensitively (first and last name joined). A booking counts while it
can still hold or has bought a seat: see ACTIVE_STATUSES and the lapsed-hold rule in `travelers_named`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text

from app.db import agencies

#: Held, pending payment, paid (payment received, paid not ticketed), confirmed or ticketed; and a group hold being placed
#: (hold_pending) or held at the supplier but not completed (hold_unrecorded). Not: requested (nothing held), hold_failed,
#: cancelled, expired.
ACTIVE_STATUSES = ("held", "pending_payment", "payment_received", "confirmed", "ticket_issued", "paid_not_ticketed",
                   "hold_pending", "hold_unrecorded")
#: A group hold record is written just before the supplier is called; one older than this was left by a crash.
PENDING_MINUTES = 60


@dataclass(frozen=True)
class ExistingTraveler:
    reference: str
    product: str
    itinerary: list[dict[str, Any]] | None
    first_name: str
    last_name: str
    date_of_birth: date | None
    document_number: str | None
    booking_status: str = "held"
    payment_status: str = "unpaid"
    channel: str = "staff"
    contact_email: str | None = None
    contact_phone: str | None = None  # phone code and number, digits only

    def __repr__(self) -> str:  # never print identity data
        return f"ExistingTraveler(reference={self.reference})"


def travelers_named(names: list[str], exclude_reference: str | None = None) -> list[ExistingTraveler]:
    """Travelers on active bookings whose joined, lower-cased, space-free name is one of `names`. An unpaid hold past its
    time limit (not yet marked expired by the expiry job) does not count."""
    if not names:
        return []
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text(
            "SELECT b.reference, b.product, b.itinerary, t.first_name, t.last_name, t.date_of_birth, t.document_number,"
            " b.booking_status, b.payment_status, b.channel, c.email, c.phone_code, c.phone"
            " FROM bookings b JOIN booking_travelers t ON t.booking_id = b.id"
            " LEFT JOIN booking_customers c ON c.booking_id = b.id"
            " WHERE b.booking_status = ANY(:active)"
            " AND (CAST(:exclude AS text) IS NULL OR b.reference <> CAST(:exclude AS text))"
            " AND NOT (b.payment_status = 'unpaid' AND b.hold_expires_at IS NOT NULL AND b.hold_expires_at < now())"
            " AND NOT (b.booking_status = 'hold_pending' AND b.created_at < now() - make_interval(mins => :pending))"
            " AND lower(regexp_replace(t.first_name || t.last_name, '\\s+', '', 'g')) = ANY(:names)"
            " ORDER BY b.created_at"),
            {"active": list(ACTIVE_STATUSES), "exclude": exclude_reference, "pending": PENDING_MINUTES, "names": names}).all()
    return [ExistingTraveler(reference=row.reference, product=row.product, itinerary=row.itinerary, first_name=row.first_name,
                             last_name=row.last_name, date_of_birth=row.date_of_birth, document_number=row.document_number,
                             booking_status=row.booking_status, payment_status=row.payment_status, channel=row.channel,
                             contact_email=(row.email or "").strip().lower() or None,
                             contact_phone="".join(ch for ch in f"{row.phone_code or ''}{row.phone or ''}" if ch.isdigit()) or None)
            for row in rows]


def record_flag(reference: str, actor: str | None, details: dict[str, Any]) -> None:
    """Audit entry `possible_double_booking` on the new booking (journeys and counts only: no names, no other reference)."""
    from app.db.supplier_bookings import _event

    with agencies.application_engine().begin() as connection:
        booking_id = connection.execute(text("SELECT id FROM bookings WHERE reference = :r"), {"r": reference}).scalar_one_or_none()
        if booking_id is not None:
            _event(connection, booking_id, actor, "possible_double_booking", details)
