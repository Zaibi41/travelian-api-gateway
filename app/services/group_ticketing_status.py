"""Group bookings after payment: "Paid – awaiting final PNR" -> "Final PNR received" -> "Ticketed".

Staff record the airline's final PNR and each traveler's ticket number (POST /v1/group-ticketing/holds/{id}/tickets).
None of the group suppliers' booking reads returns them today (Dhillu & Sahi agent-booking-detail has the group PNR
only, no final PNR or ticket numbers; docs/backend-todo.md). The itinerary / e-ticket prints only once Ticketed.
"""

from __future__ import annotations

from typing import Literal

TicketingStatus = Literal["awaiting_final_pnr", "final_pnr_received", "ticketed"]

LABEL: dict[str, str] = {
    "awaiting_final_pnr": "Paid – awaiting final PNR",
    "final_pnr_received": "Final PNR received",
    "ticketed": "Ticketed",
}

#: Statuses with nothing left to ticket.
_CLOSED = ("cancelled", "expired", "hold_unrecorded", "hold_pending", "hold_failed")


def ticketing_status(booking_status: str, issuance_status: str | None, final_pnr: str | None, paid_in_full: bool) -> TicketingStatus | None:
    """None before full payment (or on a closed booking); Ticketed needs the final PNR and every ticket number."""
    if booking_status in _CLOSED:
        return None
    if final_pnr and (booking_status == "ticket_issued" or issuance_status == "issued"):
        return "ticketed"
    if final_pnr:
        return "final_pnr_received"
    return "awaiting_final_pnr" if paid_in_full else None


#: The group itinerary / e-ticket is refused before this, and its text.
ITINERARY_NOT_YET = "Available once the tickets are issued (final PNR)"
