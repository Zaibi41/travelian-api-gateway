from fastapi import APIRouter

from app.core.config import settings
from app.schemas.flights import FlightSearchRequest, FlightSearchResponse
from app.services.flight_search import FlightSearchService
from app.vendors.base import FlightVendor
from app.vendors.demo import DemoVendor

router = APIRouter(prefix="/live-ticketing", tags=["live ticketing"])


def get_vendors() -> list[FlightVendor]:
    """Return configured live flight supplier adapters."""
    vendors: list[FlightVendor] = []
    if settings.enable_demo_vendor:
        vendors.append(DemoVendor())
    # Dhillu & Sahi currently has login documentation only. Register its flight
    # search adapter here once the supplier shares its search endpoint contract.
    return vendors


@router.post("/flights/search", response_model=FlightSearchResponse)
async def search_flights(request: FlightSearchRequest) -> FlightSearchResponse:
    """Search configured live-ticketing flight suppliers concurrently."""
    return await FlightSearchService(get_vendors()).search(request)
