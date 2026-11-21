"""No double booking (issue #17): the same passenger on the same route and departure date twice.

Checked before anything is held or saved, on every creation path: staff and website flight holds (Southair, SooperFare),
flight booking requests, and staff and website group holds and requests. Flights and group tickets are checked against
each other.

- Same passenger: first + last name equal ignoring case and spaces, AND the same date of birth or the same passport /
  CNIC number (when both bookings have one). Adults, children and infants alike.
- Same route: a journey of the new booking (outbound, and the return of a round trip) has the same origin, destination
  and departure date as a journey of an active booking (app.db.duplicate_bookings.ACTIVE_STATUSES).
- Refused with 409 `duplicate_booking`. Staff see the existing booking's reference; the website gets a plain sentence
  with no other booking's details.

Two identical requests arriving together are refused too: while a check-and-book is running for a passenger and journey,
another one for the same passenger and journey (in this gateway process) is refused.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Iterator

from starlette.concurrency import run_in_threadpool

from app.db import duplicate_bookings as store

logger = logging.getLogger(__name__)

CODE = "duplicate_booking"
WEBSITE_MESSAGE = ("One of these travelers already has a booking on this route and date, so nothing was booked. "
                   "If you need help, contact Travelian.")
CHECK_FAILED = "Existing bookings couldn't be checked right now, so nothing was booked. Try again."

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


def _message(person: Person, reference: str, journey: Journey, staff: bool, nothing: str) -> str:
    if not staff:
        return WEBSITE_MESSAGE
    name = " ".join(f"{person.first_name} {person.last_name}".split())
    origin, destination, day = journey
    return (f"{name} already has booking {reference} on {origin} → {destination} on {_text_date(day)}. "
            f"A second booking for the same passenger, route and date isn't allowed. {nothing}")


def find(passengers: list[Person], journeys: set[Journey], existing: list[store.ExistingTraveler]) -> tuple[Person, str, Journey] | None:
    """The first passenger who already travels on one of these journeys: (passenger, existing reference, journey)."""
    for traveler in existing:
        theirs = Person(traveler.first_name, traveler.last_name, traveler.date_of_birth, traveler.document_number)
        shared = itinerary_journeys(traveler.itinerary) & journeys
        if not shared:
            continue
        for passenger in passengers:
            if same_person(passenger, theirs):
                return passenger, traveler.reference, sorted(shared)[0]
    return None


# Check-and-book calls now running in this process: (name key, journey) -> the passengers being booked.
_in_flight: dict[tuple[str, Journey], list[Person]] = {}


@contextmanager
def booking(passengers: Iterable[Any], journeys: set[Journey], *, staff: bool, nothing: str = "Nothing was booked.") -> Iterator[None]:
    """Hold this passenger and journey while the booking is being made: a second identical call meanwhile is refused.
    Use around the whole check-and-book, after `check`."""
    persons = [person for person in people(passengers) if person.key]
    for person in persons:
        for journey in sorted(journeys):
            if any(same_person(person, other) for other in _in_flight.get((person.key, journey), [])):
                raise DuplicateBooking(409, WEBSITE_MESSAGE if not staff else
                                       f"{' '.join(f'{person.first_name} {person.last_name}'.split())} is already being booked on "
                                       f"{journey[0]} → {journey[1]} on {_text_date(journey[2])}. {nothing}")
    added: list[tuple[tuple[str, Journey], Person]] = []
    for person in persons:
        for journey in sorted(journeys):
            _in_flight.setdefault((person.key, journey), []).append(person)
            added.append(((person.key, journey), person))
    try:
        yield
    finally:
        for key, person in added:
            entries = [entry for entry in _in_flight.get(key, []) if entry is not person]
            if entries:
                _in_flight[key] = entries
            else:
                _in_flight.pop(key, None)


async def check(passengers: Iterable[Any], journeys: set[Journey], *, staff: bool, exclude_reference: str | None = None,
                nothing: str = "Nothing was booked.") -> None:
    """Refuse (DuplicateBooking, 409) when a passenger already has an active booking on one of these journeys.
    exclude_reference: the booking being re-held itself. Fails closed (503) when bookings can't be read."""
    persons = [person for person in people(passengers) if person.key]
    if not persons or not journeys:
        return
    try:
        existing = await run_in_threadpool(store.travelers_named, sorted({person.key for person in persons}), exclude_reference)
    except Exception as error:  # noqa: BLE001 - never book without the check
        logger.error("Double-booking check failed; nothing booked", extra={"error_type": type(error).__name__})
        raise DuplicateBooking(503, CHECK_FAILED, "duplicate_check_failed") from None
    found = find(persons, journeys, existing)
    if found is None:
        return
    person, reference, journey = found
    logger.info("Double booking refused", extra={"existing_reference": reference})
    raise DuplicateBooking(409, _message(person, reference, journey, staff, nothing), reference=reference if staff else None)
