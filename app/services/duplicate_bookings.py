"""No double booking (issue #17): the same passenger on the same route and departure date twice.

Checked before anything is held or saved, on every creation path: staff and website flight holds (Southair, SooperFare),
flight booking requests, and staff and website group holds and requests. Flights and group tickets are checked against
each other.

- Same passenger: first + last name equal ignoring case and spaces, AND the same date of birth or the same passport /
  CNIC number (when both bookings have one). Adults, children and infants alike.
- Same route: a journey of the new booking (outbound, and the return of a round trip) has the same origin, destination
  and departure date as a journey of an active booking (app.db.duplicate_bookings.ACTIVE_STATUSES).
- Who is refused by what (see `decide`): the website never (a match is accepted and flagged for staff: an audit entry
  and a staff notification, no difference in the public answer); staff by a paid, confirmed or ticketed booking or a
  staff-made hold, while an unpaid website hold is a warning.
- Refused with 409 `duplicate_booking`. Staff see the existing booking's reference only when they may see that booking;
  staff notifications never carry it.

Two identical staff requests arriving together are refused too (in this gateway process).
"""

from __future__ import annotations

import asyncio
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
    """Staff only (the website is never refused)."""
    if reference_visible(row, viewer):
        return DuplicateBooking(409, f"{_name(person)} already has booking {row.reference} on {_route(journey)}. "
                                     f"A second booking for the same passenger, route and date isn't allowed. {nothing}", reference=row.reference)
    return DuplicateBooking(409, f"{_name(person)} already has a booking on {_route(journey)}. "
                                 f"A second booking for the same passenger, route and date isn't allowed. {nothing}")


@dataclass(frozen=True)
class Flag:
    """A possible double booking that was let through: the new passenger, the existing booking row and the journey."""
    person: Person
    row: store.ExistingTraveler
    journey: Journey

    def __repr__(self) -> str:  # never print identity data
        return f"Flag(journey={self.journey})"


CHECK_ADVICE = "Check with the customer that it isn't the same trip before taking payment."


def warnings_for(flags: list[Flag], viewer: Any) -> list[str]:
    """The warnings in a staff viewer's own API response: the existing reference only when that viewer may see it. The
    website never gets them."""
    if is_website(viewer):
        return []
    texts = []
    for flag in flags:
        booking = f"an unpaid website hold ({flag.row.reference})" if reference_visible(flag.row, viewer) else "an unpaid website hold"
        texts.append(f"Possible double booking: {_name(flag.person)} has {booking} on {_route(flag.journey)}. {CHECK_ADVICE}")
    return list(dict.fromkeys(texts))


def notification_note(flags: list[Flag]) -> str:
    """For the staff notification and email: never a reference (its readers may not be allowed to see that booking)."""
    lines = [f"Possible double booking: {_name(flag.person)} already has an active booking on {_route(flag.journey)}."
             for flag in flags]
    return " ".join([*dict.fromkeys(lines), CHECK_ADVICE])


def matches(passengers: list[Person], journeys: set[Journey], existing: list[store.ExistingTraveler]) -> list[Flag]:
    """Each passenger who already travels on one of these journeys, with the existing booking row and the journey."""
    found = []
    for traveler in existing:
        theirs = Person(traveler.first_name, traveler.last_name, traveler.date_of_birth, traveler.document_number)
        shared = itinerary_journeys(traveler.itinerary) & journeys
        if not shared:
            continue
        for passenger in passengers:
            if same_person(passenger, theirs):
                found.append(Flag(passenger, traveler, sorted(shared)[0]))
    return found


def decide(passengers: list[Person], journeys: set[Journey], existing: list[store.ExistingTraveler], viewer: Any,
           nothing: str = "Nothing was booked.") -> list[Flag]:
    """The rule (issue #17 and its two security reviews). Raises DuplicateBooking (staff only), or returns the flags.

    - Website: never refused. Contact details a guest types prove nothing, so any answer that depended on another booking
      would tell a stranger whether someone is booked. Every match is accepted and flagged for staff.
    - Staff: refused by a paid, confirmed or ticketed booking, or by a staff-made hold; an unpaid website hold (anyone can
      type a name on the website) is flagged, not refused. The existing reference is named only when the viewer may see it."""
    found = matches(passengers, journeys, existing)
    if is_website(viewer):
        return found
    for flag in found:
        if blocks_staff(flag.row):
            raise _refusal(flag.person, flag.row, flag.journey, viewer, nothing)
    return found


async def flag_for_staff(reference: str, flags: list[Flag], *, actor: str | None, passenger: str | None = None) -> None:
    """Tell staff about a possible double booking that was let through: an audit entry on the new booking (no personal
    data, no other reference) and a staff notification (no reference of the other booking). Never raises."""
    if not flags:
        return
    from app.services import notifications

    details = {"journeys": sorted({f"{o}-{d} {day}" for o, d, day in (flag.journey for flag in flags)}),
               "matches": len(flags), "existing_paid": any(paid_or_confirmed(flag.row) for flag in flags)}
    try:
        await run_in_threadpool(store.record_flag, reference, actor, details)
    except Exception as error:  # noqa: BLE001 - the booking is saved; the flag must not break it
        logger.error("Possible double booking not recorded", extra={"reference": reference, "error_type": type(error).__name__})
    first = flags[0].journey
    await notifications.booking_event("possible_double_booking", reference=reference, passenger=passenger or _name(flags[0].person),
                                      route=f"{first[0]} → {first[1]}", fare_minor=None, currency=None, pnr=None, hold_expires_at=None,
                                      note=notification_note(flags))


#: Website flag tasks still running (kept so they aren't garbage-collected before they finish).
_background: set[asyncio.Task] = set()


async def flag(reference: str, flags: list[Flag], *, viewer: Any, actor: str | None, passenger: str | None = None) -> None:
    """Flag for staff. Staff: now. Website: always scheduled in the background (even with nothing to flag), so the guest's
    response time never depends on whether another booking matched."""
    if not is_website(viewer):
        await flag_for_staff(reference, flags, actor=actor, passenger=passenger)
        return

    async def run() -> None:
        try:
            await flag_for_staff(reference, flags, actor=actor, passenger=passenger)
        except Exception as error:  # noqa: BLE001 - background work never surfaces to the guest
            logger.error("Background double-booking flag failed", extra={"reference": reference, "error_type": type(error).__name__})

    task = asyncio.get_running_loop().create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)


# Check-and-book calls by staff now running in this process: (name key, journey) -> the passengers being booked.
_in_flight: dict[tuple[str, Journey], list[Person]] = {}


@contextmanager
def booking(passengers: Iterable[Any], journeys: set[Journey], *, viewer: Any, nothing: str = "Nothing was booked.") -> Iterator[None]:
    """Staff: hold this passenger and journey while the booking is being made; a second identical staff call meanwhile is
    refused. Use around the whole check-and-book. The website is never refused, so this does nothing for it."""
    if is_website(viewer):
        yield
        return
    persons = [person for person in people(passengers) if person.key]
    for person in persons:
        for journey in sorted(journeys):
            if any(same_person(person, other) for other in _in_flight.get((person.key, journey), [])):
                raise DuplicateBooking(409, f"{_name(person)} is already being booked on {_route(journey)}. {nothing}")
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


async def check(passengers: Iterable[Any], journeys: set[Journey], *, viewer: Any, exclude_reference: str | None = None,
                nothing: str = "Nothing was booked.") -> list[Flag]:
    """Refuse (DuplicateBooking, 409) per `decide`, or return the flags to pass to `warnings_for` / `flag_for_staff`.
    exclude_reference: the booking being re-held itself. Staff: fails closed (503) when bookings can't be read; the website
    is never refused, so then it goes ahead unflagged (logged)."""
    persons = [person for person in people(passengers) if person.key]
    if not persons or not journeys:
        return []
    try:
        existing = await run_in_threadpool(store.travelers_named, sorted({person.key for person in persons}), exclude_reference)
    except Exception as error:  # noqa: BLE001
        logger.error("Double-booking check failed", extra={"error_type": type(error).__name__, "website": is_website(viewer)})
        if is_website(viewer):
            return []
        raise DuplicateBooking(503, CHECK_FAILED, "duplicate_check_failed") from None
    try:
        return decide(persons, journeys, existing, viewer, nothing)
    except DuplicateBooking as error:
        logger.info("Double booking refused", extra={"existing_reference": error.reference})
        raise
