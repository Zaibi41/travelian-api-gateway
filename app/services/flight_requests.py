"""Request to book (B6): restricted airlines (D32), or any fare a customer asks Travelian to arrange.

Saved as a booking with status 'requested' from the revalidated quote: customer, travelers, flights and the quoted price.
**No supplier call of any kind** (no search, quote, hold or book). Staff are notified at once; they confirm the fare
and payment with the customer outside the system and then make the booking themselves in the admin.
"""

from __future__ import annotations

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BACK_OFFICE_ROLES, BackOfficeUser
from app.db import supplier_bookings
from app.schemas.live_flights import FlightMoney, HoldRequest
from app.services import flight_holds, live_flights, notifications, restricted_airlines

NOT_HELD = ("This booking is not held or confirmed. Travelian's team will contact you to confirm the fare and arrange "
            "payment; the seats are only booked after that.")


class RequestView(BaseModel):
    booking_id: str
    status: str = "requested"
    selling_total: FlightMoney
    message: str = NOT_HELD


async def create(request: HoldRequest, viewer: BackOfficeUser, channel: str = "staff") -> RequestView:
    HoldError = flight_holds.HoldError
    if channel == "staff" and viewer.role not in BACK_OFFICE_ROLES:
        raise HoldError(403, "Only back-office users can send booking requests here.", code="not_allowed")
    quote = live_flights.get_quote(request.quote_id, viewer.user_id)
    if quote is None:
        raise HoldError(404, "The revalidated fare has expired. Revalidate again.", code="quote_expired")
    counts = {"ADT": quote.request.adults, "CHD": quote.request.children, "INF": quote.request.infants}
    if {kind: sum(p.type == kind for p in request.passengers) for kind in counts} != counts:
        raise HoldError(422, "The passengers must match the search (adults, children, infants).", code="passengers_mismatch")
    request, links = await run_in_threadpool(flight_holds.resolve_saved, request, None)
    restricted = any(restricted_airlines.segment_restricted(ref.segment) for ref in quote.refs)
    selling = {kind: selling for kind, _, _, selling in quote.passengers}
    costs = {kind: cost for kind, _, cost, _ in quote.passengers}
    from dataclasses import replace

    travelers = [replace(t, selling_minor=selling.get(t.passenger_type), cost_minor=costs.get(t.passenger_type))
                 for t in flight_holds._travelers(request, links)]
    reference = await run_in_threadpool(lambda: supplier_bookings.save_request(
        provider=quote.provider, currency=quote.currency, selling_total_minor=quote.selling_total_minor,
        supplier_total_minor=quote.supplier_total_minor, actor_user_id=viewer.user_id if channel == "staff" else None,
        customer=flight_holds._customer(request), travelers=travelers, itinerary=[ref.segment for ref in quote.refs if ref.segment],
        details={"restricted_airline": restricted, "quoted_at_selling_total_minor": quote.selling_total_minor}, channel=channel,
    ))
    await notifications.booking_event(
        "restricted_request" if restricted else "booking_requested", reference=reference, passenger=flight_holds._lead_name(request),
        route=flight_holds._route(quote.refs), fare_minor=quote.selling_total_minor, currency=quote.currency, pnr=None,
        hold_expires_at=None, note="Not held at the supplier. Contact the customer to confirm the fare and payment, then book it in the admin.",
    )
    return RequestView(booking_id=reference, selling_total=FlightMoney(amount_minor=quote.selling_total_minor, currency=quote.currency))
