"""Group fare terms: group fares are non-refundable and non-changeable. The customer is told before booking and the
acknowledgement is stored with the booking as a booking event: who ticked it, when, and the exact text and version shown.
The wording lives here only, so a later change gets a new version and old bookings keep the text they were shown."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: The version of the wording below. Change both together.
VERSION = "group-fare-terms-v1"
#: The staff tick box on the admin hold form, word for word.
STAFF_TEXT = "The customer has been told that this group fare is non-refundable and non-changeable, and agrees."
#: Next to the fare on printed documents.
TAG = "Non-refundable · Non-changeable"
#: The full line: the notice above group results and the first term in a group booking's terms box.
TERM = "Group fares are non-refundable and non-changeable. Names and dates cannot be altered after booking."
EVENT = "fare_terms_acknowledged"
REQUIRED = "Confirm that the customer has been told this group fare is non-refundable and non-changeable. Nothing was held."


def event_details() -> dict[str, Any]:
    """What is stored when staff tick the box (the event itself carries the user and the time)."""
    return {"terms_version": VERSION, "terms_text": STAFF_TEXT, "acknowledged_by": "staff"}


@dataclass(frozen=True)
class Acknowledgement:
    text: str
    version: str
    acknowledged_by: str            # "staff" (admin hold form); "customer" when a public form exists
    actor_user_id: str | None
    at: datetime


def acknowledgement(events) -> Acknowledgement | None:
    """The stored acknowledgement among a booking's events (app.db.booking_people.AuditEvent), or None."""
    for event in events or []:
        if event.action == EVENT:
            details = event.details or {}
            return Acknowledgement(details.get("terms_text") or STAFF_TEXT, details.get("terms_version") or VERSION,
                                   details.get("acknowledged_by") or "staff", event.actor_user_id, event.created_at)
    return None
