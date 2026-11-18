"""Live flight search (Southair). Sign-in required: internal or agency role (customers 403).

What each caller receives is decided in app.services.live_flights (D23): opaque IDs,
selling price; supplier name and cost only with permission; NLD and fare hashes never.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.core.field_visibility import BackOfficeUser, require_back_office, require_search_viewer
from app.schemas.flights import FlightSearchRequest
from app.schemas.live_flights import FlightSearchView, HoldRequest, HoldView, RepriceRequest, RepriceView
from app.services import flight_holds, flight_requests, live_flights

router = APIRouter(
    prefix="/live-ticketing",
    tags=["live ticketing"],
    responses={401: {"description": "Not signed in."}, 403: {"description": "Role may not search."}},
)
Viewer = Annotated[BackOfficeUser, Depends(require_search_viewer)]


@router.post("/flights/search", response_model=FlightSearchView, response_model_exclude_none=True)
async def search_flights(request: FlightSearchRequest, viewer: Viewer) -> FlightSearchView:
    """Search the flight provider. Fare IDs stay valid for 15 minutes, for the user who searched."""
    return await live_flights.search(request, viewer)


@router.post(
    "/flights/reprice", response_model=RepriceView, response_model_exclude_none=True,
    responses={404: {"description": "Search expired."}, 409: {"description": "Fare gone or no selling price."}, 422: {"description": "Selection or passenger mix can't be priced."}, 502: {"description": "Supplier failed."}},
)
async def reprice_flights(request: RepriceRequest, viewer: Viewer) -> RepriceView:
    """Repeat the search for the selected fares and return the total to confirm (valid 10 minutes)."""
    try:
        return await live_flights.reprice(request.search_id, request.fare_ids, viewer)
    except live_flights.RepriceError as error:
        # detail: the sentence to show; code: a stable reason the page can act on.
        return JSONResponse({"detail": error.message, "code": error.code}, status_code=error.status)


@router.post(
    "/flights/bookings", response_model=HoldView, status_code=201,
    responses={409: {"description": "Booking turned off, price changed, or no selling price."}, 404: {"description": "Quote expired."},
               422: {"description": "Passengers don't match."}, 502: {"description": "Supplier failed; nothing saved."}},
)
async def hold_flight(request: HoldRequest, viewer: Annotated[BackOfficeUser, Depends(require_back_office)]) -> HoldView:
    """Hold only (no ticketing, no payment), when booking is turned on. Saved as UAT test data."""
    try:
        return await flight_holds.hold(request, viewer)
    except flight_holds.HoldError as error:
        # detail: the sentence to show; code: a stable reason; pnr / booking_id when a hold exists.
        if error.field_errors:  # per-field messages, shaped like request validation errors
            detail = [{"loc": ["body", *path], "msg": message, "type": "value_error"} for path, message in error.field_errors]
            return JSONResponse({"detail": detail, "code": error.code, "message": error.message, **error.extra}, status_code=error.status)
        return JSONResponse({"detail": error.message, "code": error.code, **error.extra}, status_code=error.status)


@router.post(
    "/flights/requests", response_model=flight_requests.RequestView, status_code=201,
    responses={404: {"description": "Quote expired."}, 422: {"description": "Passengers don't match."}},
)
async def request_flight(request: HoldRequest, viewer: Annotated[BackOfficeUser, Depends(require_back_office)]):
    """Request to book (restricted airlines, D32): saved for staff to arrange; never a supplier hold or booking call."""
    try:
        return await flight_requests.create(request, viewer)
    except flight_holds.HoldError as error:
        return JSONResponse({"detail": error.message, "code": error.code, **error.extra}, status_code=error.status)
