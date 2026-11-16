"""Group Ticketing holds (admin, back office only: super_admin and staff; others 403).

Hold seats, read the hold, re-check the price, release, issue. Supplier names only for viewers
with the supplier names permission; supplier cost never. Errors carry `detail` (the sentence to
show) and `code` (a stable reason); field errors are shaped like request validation errors.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse

from app.core.field_visibility import BackOfficeUser, require_back_office
from app.schemas.group_holds import GroupHoldRequest, GroupHoldView, GroupIssueRequest, GroupIssueResult, GroupPriceCheck, GroupTicketsRequest
from app.services import group_holds

router = APIRouter(
    prefix="/group-ticketing/holds",
    tags=["group ticketing holds"],
    responses={401: {"description": "Not signed in."}, 403: {"description": "Not back office, or missing the permission."},
               404: {"description": "No such group booking."}},
)
Staff = Annotated[BackOfficeUser, Depends(require_back_office)]
Reference = Annotated[str, Path(max_length=64)]


def _error(error: group_holds.GroupHoldError) -> JSONResponse:
    if error.field_errors:
        detail = [{"loc": ["body", *path], "msg": message, "type": "value_error"} for path, message in error.field_errors]
        return JSONResponse({"detail": detail, "code": error.code, "message": error.message, **error.extra}, status_code=error.status)
    return JSONResponse({"detail": error.message, "code": error.code, **error.extra}, status_code=error.status)


@router.post(
    "", response_model=GroupHoldView, response_model_exclude_none=True, status_code=201,
    responses={409: {"description": "Fare gone, not enough seats, holds not supported for this fare, or no selling price. Nothing held."},
               422: {"description": "Adults only; passengers must match the adults searched."},
               502: {"description": "The supplier didn't confirm the hold."}},
)
async def hold_group_seats(request: GroupHoldRequest, viewer: Staff):
    """Hold the seats with the supplier (never retried). The answer carries the price per adult and total."""
    try:
        return await group_holds.hold(request, viewer)
    except group_holds.GroupHoldError as error:
        return _error(error)


@router.get("/{booking_id}", response_model=GroupHoldView, response_model_exclude_none=True)
async def group_hold(booking_id: Reference, viewer: Staff):
    """The hold with its countdown, price, payments and masked travelers. A hold past its time limit reads as expired."""
    try:
        return await group_holds.read(booking_id, viewer)
    except group_holds.GroupHoldError as error:
        return _error(error)


@router.post("/{booking_id}/price-check", response_model=GroupPriceCheck,
             responses={409: {"description": "Not on hold or expired."}, 502: {"description": "The supplier couldn't be asked."}})
async def check_group_price(booking_id: Reference, viewer: Staff):
    """Ask the supplier for the current price (read-only). Nothing changes."""
    try:
        return await group_holds.price_check(booking_id, viewer)
    except group_holds.GroupHoldError as error:
        return _error(error)


@router.post("/{booking_id}/accept-held-price", response_model=GroupHoldView, response_model_exclude_none=True,
             responses={403: {"description": "Needs the record_payments permission."}, 409: {"description": "No price change to confirm, or not on hold."}})
async def accept_held_price(booking_id: Reference, viewer: Staff):
    """The customer agreed to the price the supplier held at (it differed from the price shown). Payments can then be recorded."""
    try:
        return await group_holds.accept_held_price(booking_id, viewer)
    except group_holds.GroupHoldError as error:
        return _error(error)


@router.post("/{booking_id}/release", response_model=GroupHoldView, response_model_exclude_none=True,
             responses={409: {"description": "Not on hold, expired, or being issued."}, 502: {"description": "The supplier didn't confirm."}})
async def release_group_hold(booking_id: Reference, viewer: Staff):
    """Release the seats at the supplier, then mark the booking released (logged)."""
    try:
        return await group_holds.release(booking_id, viewer)
    except group_holds.GroupHoldError as error:
        return _error(error)


@router.post("/{booking_id}/issue", response_model=GroupIssueResult, response_model_exclude_none=True,
             responses={409: {"description": "supplier_cost_changed or below_cost (with the price check's fields), payment_due, hold_expired, or not issuable."},
                        502: {"description": "The price couldn't be checked. Nothing was issued."}})
async def issue_group_tickets(request: GroupIssueRequest, booking_id: Reference, viewer: Staff):
    """Check the supplier cost again; the selling price never changes. If the cost moved, refuse with supplier_cost_changed
    unless accepted_supplier_cost equals the new cost (and accept_below_cost when it is above the selling price).
    Needs full payment and the issue tickets permission. One issue call, never retried."""
    try:
        return await group_holds.issue(booking_id, viewer, request.accepted_supplier_cost, request.accept_below_cost)
    except group_holds.GroupHoldError as error:
        return _error(error)


@router.post("/{booking_id}/tickets", response_model=GroupHoldView, response_model_exclude_none=True,
             responses={409: {"description": "payment_due, hold_expired or not_ticketable."},
                        422: {"description": "A bad final PNR or ticket number, a repeated ticket number, or a traveler not on the booking."}})
async def record_group_tickets(request: GroupTicketsRequest, booking_id: Reference, viewer: Staff):
    """Record the airline's final PNR and per-traveler ticket numbers (issue tickets permission, paid in full; logged).
    With a ticket number for every traveler the booking becomes Ticketed and its itinerary can be printed."""
    try:
        return await group_holds.record_tickets(booking_id, viewer, request.final_pnr,
                                                {ticket.position: ticket.ticket_number for ticket in request.tickets})
    except group_holds.GroupHoldError as error:
        return _error(error)
