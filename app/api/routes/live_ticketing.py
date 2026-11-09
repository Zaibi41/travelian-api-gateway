"""Live flight search (Southair). Sign-in required: internal or agency role (customers 403).

What each caller receives is decided in app.services.live_flights (D23): opaque IDs,
selling price; supplier name and cost only with permission; NLD and fare hashes never.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.field_visibility import BackOfficeUser, require_search_viewer
from app.schemas.flights import FlightSearchRequest
from app.schemas.live_flights import FlightSearchView
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
