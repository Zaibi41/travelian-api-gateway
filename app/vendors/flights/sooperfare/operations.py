"""Every SooperFare partner call, as listed in SooperFare's Postman collection ("SooperFare Shareable").

Used by the raw supplier console (app/api/routes/sooperfare_supplier.py): one row per call, same method and path as
SooperFare's. Login is not here: the gateway logs in with its own credentials and never hands out the token.

`writes`: the call changes a real booking at SooperFare (hold seats, add paid extras, issue/charge, cancel, void).
The gateway only sends these when SOOPERFARE_RAW_WRITE_ENABLED is on, and never retries them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Operation:
    key: str
    title: str
    method: Literal["GET", "POST", "PATCH"]
    path: str
    writes: bool = False


OPERATIONS: tuple[Operation, ...] = (
    Operation("airports", "Airports", "GET", "/v1/airline/airports"),
    Operation("availability", "Availability", "GET", "/v1/airline/availability"),
    Operation("search", "Search", "POST", "/v1/airline/search"),
    Operation("quote", "Quote", "POST", "/v1/airline/quote"),
    Operation("ancillary", "Ancillary (get)", "POST", "/v1/airline/ancillary"),
    # Picks extras on the quote before a reservation; no booking exists yet, nothing is charged.
    Operation("ancillary_select", "Ancillary (select)", "PATCH", "/v1/airline/ancillary"),
    Operation("fares", "Fares On Request", "POST", "/v1/airline/fares"),
    Operation("reservation", "Reservation", "POST", "/v1/airline/reservation", writes=True),
    Operation("booking_extras", "Get Extras", "POST", "/v1/booking/extras"),
    Operation("booking_extras_submit", "Submit Extras", "PATCH", "/v1/booking/extras", writes=True),
    Operation("booking_list", "List", "POST", "/v1/booking/list"),
    Operation("booking_confirm", "Confirm", "POST", "/v1/booking/confirm", writes=True),
    Operation("booking_cancel", "Cancel", "POST", "/v1/booking/cancel", writes=True),
    Operation("booking_void", "Void", "POST", "/v1/booking/void", writes=True),
)
