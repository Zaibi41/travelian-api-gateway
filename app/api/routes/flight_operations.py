"""Flight Operations lists (read-only, back office only: super_admin and staff; others 403).

Group departures without final PNR: paid group bookings with no final PNR whose first flight leaves within the next
48 hours, soonest first. No supplier names or supplier PNRs.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from app.api.routes.admin_dashboard import Viewer, _now, _read_bookings
from app.core.config import settings
from app.db.bookings import BookingRow
from app.schemas.flight_operations import GroupDeparture, GroupDeparturesWithoutFinalPnr
from app.services.admin_bookings import customer_or_agency, route_of

router = APIRouter(prefix="/admin/flight-operations", tags=["flight operations"],
                   responses={403: {"description": "Not back office."}, 503: {"description": "Booking data is not available."}})

WINDOW_HOURS = 48
#: Nothing left to do for these: cancelled, ticketed, or never a booking.
_DONE = ("cancelled", "ticket_issued", "hold_unrecorded")


def departure_at(booking: BookingRow) -> datetime | None:
    segment = (booking.itinerary or [{}])[0]
    try:
        value = datetime.fromisoformat(str(segment.get("departure_local") or ""))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(settings.dashboard_timezone))


def group_departures_without_final_pnr(rows: list[BookingRow], now: datetime, hours: int = WINDOW_HOURS) -> GroupDeparturesWithoutFinalPnr:
    items = []
    for booking in rows:
        if booking.product != "group" or booking.payment_status != "paid" or booking.final_pnr or booking.status in _DONE:
            continue
        leaves = departure_at(booking)
        if leaves is None or not now <= leaves <= now + timedelta(hours=hours):
            continue
        first = (booking.itinerary or [{}])[0]
        items.append(GroupDeparture(booking_id=booking.booking_id, departure_at=leaves, route=route_of(booking.itinerary),
                                    airline_name=first.get("airline_name"), flight_number=first.get("flight_number"),
                                    customer_or_agency=customer_or_agency(booking)))
    items.sort(key=lambda item: (item.departure_at, item.booking_id))
    return GroupDeparturesWithoutFinalPnr(window_hours=hours, total=len(items), items=items)


@router.get("/group-departures-without-final-pnr", response_model=GroupDeparturesWithoutFinalPnr, response_model_exclude_none=True)
async def group_departures(viewer: Viewer):
    """Paid group bookings without a final PNR departing within 48 hours, soonest first."""
    rows = await run_in_threadpool(_read_bookings)
    return group_departures_without_final_pnr(rows, _now())
