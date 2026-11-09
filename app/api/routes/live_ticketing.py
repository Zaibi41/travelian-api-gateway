"""Live flight search (Southair). Sign-in required: internal or agency role (customers 403).

What each caller receives is decided in app.services.live_flights (D23): opaque IDs,
selling price; supplier name and cost only with permission; NLD and fare hashes never.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.core.field_visibility import BackOfficeUser, require_search_viewer
from app.schemas.flights import FlightSearchRequest
from app.schemas.live_flights import FlightSearchView, RepriceRequest, RepriceView
from app.services import live_flights

router = APIRouter(
    prefix="/live-ticketing",
    tags=["live ticketing"],
    responses={401: {"description": "Not signed in."}, 403: {"description": "Role may not search."}},
)
Viewer = Annotated[BackOfficeUser, Depends(require_search_viewer)]


@router.post("/flights/search", response_model=FlightSearchView, response_model_exclude_none=True)
async def search_flights(request: FlightSearchRequest, viewer: Viewer) -> FlightSearchView:
    """Search Southair. Fare IDs stay valid for 15 minutes, for the user who searched."""
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
        raise HTTPException(error.status, error.message) from None
