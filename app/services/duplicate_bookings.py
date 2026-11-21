"""No double booking (issue #17): the same passenger on the same route and departure date twice.

Checked before anything is held or saved, on every creation path: staff and website flight holds (Southair, SooperFare),
flight booking requests, and staff and website group holds and requests. Flights and group tickets are checked against
each other.

- Same passenger: first + last name equal ignoring case and spaces, AND the same date of birth or the same passport /
  CNIC number (when both bookings have one). Adults, children and infants alike.
- Same route: a journey of the new booking (outbound, and the return of a round trip) has the same origin, destination
  and departure date as a journey of an active booking (app.db.duplicate_bookings.ACTIVE_STATUSES).
- Who is refused by what (see `decide`): the website only by the same contact's own booking; staff by a paid, confirmed
  or ticketed booking or a staff-made hold, while an unpaid website hold only warns them.
- Refused with 409 `duplicate_booking`. Staff see the existing booking's reference when they may see that booking; the
  website gets a plain sentence with no other booking's details.

Two identical requests arriving together are refused too: while a check-and-book is running for a passenger and journey,
another one for the same passenger and journey (staff, or the same website contact, in this gateway process) is refused.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Iterator

from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BACK_OFFICE_ROLES
from app.db import duplicate_bookings as store

logger = logging.getLogger(__name__)

CODE = "duplicate_booking"
WEBSITE_MESSAGE = ("One of these travelers already has a booking on this route and date, so nothing was booked. "
                   "If you need help, contact Travelian.")
CHECK_FAILED = "Existing bookings couldn't be checked right now, so nothing was booked. Try again."

#: The public website's viewer role (app.services.flight_holds.WEBSITE_ROLE).
WEBSITE_ROLE = "website"
#: Paid or confirmed: these always block staff, whoever made the booking.
PAID_STATUSES = ("payment_received", "confirmed", "ticket_issued", "paid_not_ticketed")

Journey = tuple[str, str, str]  # (origin, destination, departure date YYYY-MM-DD)


class DuplicateBooking(Exception):
    """status, message (for this viewer), code; reference: the existing booking (staff only, None on the website)."""

    def __init__(self, status: int, message: str, code: str = CODE, reference: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.reference = reference


def normal_name(first_name: str | None, last_name: str | None) -> str:
    return "".join(f"{first_name or ''}{last_name or ''}".split()).lower()


def normal_document(number: str | None) -> str | None:
    cleaned = "".join(character for character in str(number or "") if character.isalnum()).upper()
    return cleaned or None


def _code(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if text and text not in ("—", "?") else None


def _day(value: Any) -> str | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def itinerary_journeys(itinerary: Iterable[dict] | None) -> set[Journey]:
    """A booking's journeys from its stored flights: per direction, the first flight's origin and departure date and the
    last flight's destination. Flights without a usable origin, destination or date are left out."""
    by_direction: dict[str, list[dict]] = {}
    for segment in itinerary or []:
        if isinstance(segment, dict):
            by_direction.setdefault(str(segment.get("direction") or "outbound"), []).append(segment)
    journeys = set()
    for segments in by_direction.values():
        origin, destination, day = _code(segments[0].get("origin")), _code(segments[-1].get("destination")), _day(segments[0].get("departure_local"))
        if origin and destination and day:
            journeys.add((origin, destination, day))
    return journeys


def search_journeys(origin: str, destination: str, departure_date: date, return_date: date | None = None) -> set[Journey]:
    """A flight search's journeys: the outbound, and the return of a round trip."""
    journeys = {(origin.upper(), destination.upper(), departure_date.isoformat())}
    if return_date is not None:
        journeys.add((destination.upper(), origin.upper(), return_date.isoformat()))
    return journeys


@dataclass(frozen=True)
class Person:
    first_name: str
    last_name: str
    date_of_birth: date | None
    document_number: str | None

    def __repr__(self) -> str:  # never print identity data
        return "Person()"

    @property
    def key(self) -> str:
        return normal_name(self.first_name, self.last_name)


def people(passengers: Iterable[Any]) -> list[Person]:
    """The identity of each passenger (HoldPassenger, TravelerRecord or anything with the same fields)."""
    return [Person(p.first_name or "", p.last_name or "", getattr(p, "date_of_birth", None), getattr(p, "document_number", None))
            for p in passengers]


def same_person(a: Person, b: Person) -> bool:
    if not a.key or a.key != b.key:
        return False
    if a.date_of_birth is not None and a.date_of_birth == b.date_of_birth:
        return True
    document = normal_document(a.document_number)
    return document is not None and document == normal_document(b.document_number)


def _text_date(day: str) -> str:
    return date.fromisoformat(day).strftime("%d %b %Y")


def _name(person: Person) -> str:
    return " ".join(f"{person.first_name} {person.last_name}".split())


def _route(journey: Journey) -> str:
    origin, destination, day = journey
    return f"{origin} → {destination} on {_text_date(day)}"


# ---------- who may be refused by which booking ----------

def is_website(viewer: Any) -> bool:
    return getattr(viewer, "role", None) == WEBSITE_ROLE


def contact_keys(contact: Any) -> tuple[str | None, str | None]:
    """(email, phone digits with code) of a HoldContact-like contact, as compared with booking_customers."""
    if contact is None:
        return None, None
    email = (getattr(contact, "email", None) or "").strip().lower() or None
    phone = "".join(ch for ch in f"{getattr(contact, 'phone_code', None) or ''}{getattr(contact, 'phone', None) or ''}" if ch.isdigit()) or None
    return email, phone


def same_contact(row: store.ExistingTraveler, email: str | None, phone: str | None) -> bool:
    return bool((email and row.contact_email == email) or (phone and row.contact_phone == phone))


def paid_or_confirmed(row: store.ExistingTraveler) -> bool:
    return row.payment_status == "paid" or row.booking_status in PAID_STATUSES


def blocks_staff(row: store.ExistingTraveler) -> bool:
    """Staff are refused by a paid / confirmed / ticketed booking, or by a hold staff made. An unpaid website hold (anyone
    can type a name on the website) only warns them."""
    return paid_or_confirmed(row) or row.channel != "website"


def reference_visible(row: store.ExistingTraveler, viewer: Any) -> bool:
    """The same rule as the admin booking list (app.services.admin_bookings.visible_to): back office only, and a hold
    Travelian couldn't record (hold_unrecorded) only for viewers with supplier access."""
    if getattr(viewer, "role", None) not in BACK_OFFICE_ROLES:
        return False
    return row.booking_status != "hold_unrecorded" or bool(getattr(getattr(viewer, "visibility", None), "supplier_names", False))


def _refusal(person: Person, row: store.ExistingTraveler, journey: Journey, viewer: Any, nothing: str) -> DuplicateBooking:
    if is_website(viewer):
        return DuplicateBooking(409, WEBSITE_MESSAGE)
    if reference_visible(row, viewer):
        return DuplicateBooking(409, f"{_name(person)} already has booking {row.reference} on {_route(journey)}. "
                                     f"A second booking for the same passenger, route and date isn't allowed. {nothing}", reference=row.reference)
    return DuplicateBooking(409, f"{_name(person)} already has a booking on {_route(journey)}. "
                                 f"A second booking for the same passenger, route and date isn't allowed. {nothing}")


def _warning(person: Person, row: store.ExistingTraveler, journey: Journey, viewer: Any) -> str:
    booking = f"an unpaid website hold ({row.reference})" if reference_visible(row, viewer) else "an unpaid website hold"
    return (f"Possible double booking: {_name(person)} has {booking} on {_route(journey)}. "
            "Check with the customer that it isn't the same trip before taking payment.")


def matches(passengers: list[Person], journeys: set[Journey], existing: list[store.ExistingTraveler]
            ) -> list[tuple[Person, store.ExistingTraveler, Journey]]:
    """Each (passenger, existing booking row, shared journey) where a passenger already travels on one of these journeys."""
    found = []
    for traveler in existing:
        theirs = Person(traveler.first_name, traveler.last_name, traveler.date_of_birth, traveler.document_number)
        shared = itinerary_journeys(traveler.itinerary) & journeys
        if not shared:
            continue
        for passenger in passengers:
            if same_person(passenger, theirs):
                found.append((passenger, traveler, sorted(shared)[0]))
    return found


def decide(passengers: list[Person], journeys: set[Journey], existing: list[store.ExistingTraveler], viewer: Any,
           contact: Any = None, nothing: str = "Nothing was booked.") -> list[str]:
    """The rule (issue #17 and its security review). Raises DuplicateBooking, or returns warnings for staff.

    - Website: refused only by a booking of the same contact (email or phone): the guest's own booking. Someone else's
      booking never changes the answer, so the website can't be used to learn who is booked or to block a stranger.
    - Staff: refused by a paid, confirmed or ticketed booking, or by a staff-made hold; an unpaid website hold is a warning.
      The existing reference is named only when the viewer may see that booking."""
    warnings: list[str] = []
    if is_website(viewer):
        email, phone = contact_keys(contact)
        for person, row, journey in matches(passengers, journeys, existing):
            if same_contact(row, email, phone):
                raise _refusal(person, row, journey, viewer, nothing)
        return warnings
    for person, row, journey in matches(passengers, journeys, existing):
        if blocks_staff(row):
            raise _refusal(person, row, journey, viewer, nothing)
        warnings.append(_warning(person, row, journey, viewer))
    return list(dict.fromkeys(warnings))


# Check-and-book calls now running in this process: (scope, name key, journey) -> the passengers being booked. The scope is
# "staff", or the website contact, so a guest's double click is refused but a stranger's booking never is.
_in_flight: dict[tuple[str, str, Journey], list[Person]] = {}


def _scope(viewer: Any, contact: Any) -> str:
    if not is_website(viewer):
        return "staff"
    email, phone = contact_keys(contact)
    return f"website:{email or ''}:{phone or ''}"


@contextmanager
def booking(passengers: Iterable[Any], journeys: set[Journey], *, viewer: Any, contact: Any = None,
            nothing: str = "Nothing was booked.") -> Iterator[None]:
    """Hold this passenger and journey while the booking is being made: a second identical call meanwhile (staff, or the
    same website contact) is refused. Use around the whole check-and-book."""
    scope = _scope(viewer, contact)
    persons = [person for person in people(passengers) if person.key]
    for person in persons:
        for journey in sorted(journeys):
            if any(same_person(person, other) for other in _in_flight.get((scope, person.key, journey), [])):
                raise DuplicateBooking(409, WEBSITE_MESSAGE if is_website(viewer) else
                                       f"{_name(person)} is already being booked on {_route(journey)}. {nothing}")
    added: list[tuple[tuple[str, str, Journey], Person]] = []
    for person in persons:
        for journey in sorted(journeys):
            _in_flight.setdefault((scope, person.key, journey), []).append(person)
            added.append(((scope, person.key, journey), person))
    try:
        yield
    finally:
        for key, person in added:
            entries = [entry for entry in _in_flight.get(key, []) if entry is not person]
            if entries:
                _in_flight[key] = entries
            else:
                _in_flight.pop(key, None)


async def check(passengers: Iterable[Any], journeys: set[Journey], *, viewer: Any, contact: Any = None,
                exclude_reference: str | None = None, nothing: str = "Nothing was booked.") -> list[str]:
    """Refuse (DuplicateBooking, 409) per `decide`, or return warnings for staff. exclude_reference: the booking being
    re-held itself. Fails closed (503) when bookings can't be read."""
    persons = [person for person in people(passengers) if person.key]
    if not persons or not journeys:
        return []
    try:
        existing = await run_in_threadpool(store.travelers_named, sorted({person.key for person in persons}), exclude_reference)
    except Exception as error:  # noqa: BLE001 - never book without the check
        logger.error("Double-booking check failed; nothing booked", extra={"error_type": type(error).__name__})
        raise DuplicateBooking(503, CHECK_FAILED, "duplicate_check_failed") from None
    try:
        return decide(persons, journeys, existing, viewer, contact, nothing)
    except DuplicateBooking as error:
        logger.info("Double booking refused", extra={"existing_reference": error.reference})
        raise
