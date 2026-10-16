from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from app.schemas.flights import FlightOffer, FlightSearchRequest, FlightSegment
from app.vendors.base import FlightVendor


class DemoVendor(FlightVendor):
    """Deterministic sample adapter; replace with a real supplier integration."""

    name = "demo"
    product = "live_ticketing"

    async def search(self, request: FlightSearchRequest) -> list[FlightOffer]:
        departure = datetime.combine(request.departure_date, time(8, 30), tzinfo=timezone.utc)
        arrival = departure + timedelta(hours=2, minutes=10)
        return [
            FlightOffer(
                id=f"demo-{request.origin}-{request.destination}-{request.departure_date.isoformat()}",
                vendor=self.name,
                validating_airline="PK",
                price=Decimal("42000.00"),
                currency=request.currency,
                seats_available=7,
                segments=[
                    FlightSegment(
                        airline="PK",
                        flight_number="PK-201",
                        origin=request.origin,
                        destination=request.destination,
                        departure_at=departure,
                        arrival_at=arrival,
                        duration_minutes=130,
                        cabin=request.cabin,
                    )
                ],
            )
        ]
