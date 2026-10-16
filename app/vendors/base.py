from abc import ABC, abstractmethod

from app.schemas.flights import FlightOffer, FlightSearchRequest


class FlightVendor(ABC):
    """Adapter contract for a flight supplier."""

    name: str
    product: str = "live_ticketing"

    @abstractmethod
    async def search(self, request: FlightSearchRequest) -> list[FlightOffer]:
        """Return normalized offers or raise an exception for supplier failures."""
