"""Super Admin corrections to a booking's travelers and customer name (travelian#225).

A correction needs a reason and is written to the booking's audit trail ("traveler_corrected" / "customer_corrected")
with who, when, which fields and why; the values themselves are never logged (docs/pii-storage.md). Money, payments,
prices and tickets are not touched here: finance entries are corrected with adjustments, never edited.
Only Travelian's record changes: a name already sent to the airline must also be corrected with the airline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text

from app.db import agencies
from app.db.supplier_bookings import _event

TRAVELER_FIELDS = ("title", "first_name", "last_name", "date_of_birth", "document_number", "document_country", "nationality", "document_expiry")
CUSTOMER_FIELDS = ("first_name", "last_name")


class CorrectionRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Corrected:
    fields: list[str]
    #: The booking already went to the airline (a PNR or ticket): the airline's record isn't changed by this.
    sent_to_airline: bool


def _booking(connection, reference: str):
    booking = connection.execute(
        text("SELECT id, pnr, booking_status FROM bookings WHERE reference = :r FOR UPDATE"), {"r": reference}).mappings().one_or_none()
    if booking is None:
        raise CorrectionRefused(404, "Booking not found.")
    return booking


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise CorrectionRefused(422, "Say why this is being corrected (at least 5 characters).")
    return reason


def correct_traveler(reference: str, position: int, changes: dict[str, Any], reason: str, actor_user_id: str,
                     today: date | None = None) -> Corrected:
    reason = _reason(reason)
    with agencies.application_engine().begin() as connection:
        booking = _booking(connection, reference)
        row = connection.execute(
            text("SELECT " + ", ".join(TRAVELER_FIELDS) + ", id_type, passenger_type, ticket_number FROM booking_travelers"
                 " WHERE booking_id = :b AND position = :p FOR UPDATE"), {"b": booking["id"], "p": position}).mappings().one_or_none()
        if row is None:
            raise CorrectionRefused(404, "No traveler at that position on this booking.")
        changed = {k: v for k, v in changes.items() if k in TRAVELER_FIELDS and v is not None and v != row[k]}
        if not changed:
            raise CorrectionRefused(409, "Nothing changed: the values are the same as on the booking.")
        after = {**dict(row), **changed}
        today = today or datetime.now(UTC).date()
        if after["date_of_birth"] > today:
            raise CorrectionRefused(422, "The date of birth can't be in the future.")
        if row["passenger_type"] == "INF" and after["title"] not in ("MISS", "MASTER"):
            raise CorrectionRefused(422, "An infant's title is Miss or Master.")
        if row["id_type"] == "NATI" and after["nationality"] == "PAK" and not re.fullmatch(r"\d{13}", after["document_number"] or ""):
            raise CorrectionRefused(422, "A Pakistani national ID (CNIC or B-form) is 13 digits.")
        sets = ", ".join(f"{k} = :{k}" for k in changed)
        connection.execute(text(f"UPDATE booking_travelers SET {sets} WHERE booking_id = :b AND position = :p"),
                           {**changed, "b": booking["id"], "p": position})
        connection.execute(text("UPDATE bookings SET updated_at = now(), revision = revision + 1 WHERE id = :b"), {"b": booking["id"]})
        fields = sorted(changed)
        _event(connection, booking["id"], actor_user_id, "traveler_corrected", {"traveler_position": position, "fields": fields, "reason": reason})
        return Corrected(fields, bool(booking["pnr"] or row["ticket_number"]))


def correct_customer(reference: str, changes: dict[str, Any], reason: str, actor_user_id: str) -> Corrected:
    reason = _reason(reason)
    with agencies.application_engine().begin() as connection:
        booking = _booking(connection, reference)
        row = connection.execute(text("SELECT first_name, last_name FROM booking_customers WHERE booking_id = :b FOR UPDATE"),
                                 {"b": booking["id"]}).mappings().one_or_none()
        if row is None:
            raise CorrectionRefused(409, "This booking has no customer record to correct.")
        changed = {k: v.strip() for k, v in changes.items() if k in CUSTOMER_FIELDS and v is not None and v.strip() != row[k]}
        if not changed:
            raise CorrectionRefused(409, "Nothing changed: the name is the same as on the booking.")
        sets = ", ".join(f"{k} = :{k}" for k in changed)
        connection.execute(text(f"UPDATE booking_customers SET {sets} WHERE booking_id = :b"), {**changed, "b": booking["id"]})
        connection.execute(text("UPDATE bookings SET updated_at = now(), revision = revision + 1 WHERE id = :b"), {"b": booking["id"]})
        fields = sorted(changed)
        _event(connection, booking["id"], actor_user_id, "customer_corrected", {"fields": fields, "reason": reason})
        return Corrected(fields, bool(booking["pnr"]))
