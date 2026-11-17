"""Hotels (spec v3.0 Search > Hotels). Sign-in required: internal or agency role (customers 403).

What each caller receives is decided in app.services.hotel_search (D23): opaque IDs and the selling
price for everyone; provider name and supplier cost only with permission; supplier hashes never.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.field_visibility import BackOfficeUser, require_search_viewer
from app.schemas.hotels import DestinationSuggestRequest, DestinationSuggestView, HotelSearchRequest, HotelSearchView
from app.services import hotel_search

router = APIRouter(
    prefix="/hotels",
    tags=["hotels"],
    responses={401: {"description": "Not signed in."}, 403: {"description": "Role may not search."}},
)
Viewer = Annotated[BackOfficeUser, Depends(require_search_viewer)]


@router.post(
    "/destinations", response_model=DestinationSuggestView,
    responses={502: {"description": "Supplier failed."}, 503: {"description": "Hotel search isn't connected."}},
)
async def suggest_destinations(request: DestinationSuggestRequest, viewer: Viewer) -> DestinationSuggestView:
    """Cities, regions and hotels matching the text, for the Destination field."""
    return await hotel_search.suggest(request.query)


@router.post(
    "/search", response_model=HotelSearchView, response_model_exclude_none=True,
    responses={422: {"description": "Invalid stay, or destination not found."}},
)
async def search_hotels(request: HotelSearchRequest, viewer: Viewer) -> HotelSearchView:
    """Hotels with their rates, cheapest first. Rate IDs stay valid for 15 minutes, for the user who searched."""
    return await hotel_search.search(request, viewer)
