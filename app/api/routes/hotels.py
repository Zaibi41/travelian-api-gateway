from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

router = APIRouter(prefix="/hotels", tags=["hotels"])


class HotelSearchRequest(BaseModel):
    destination: str = Field(min_length=2, max_length=120, examples=["Dubai"])
    check_in: date
    check_out: date
    rooms: int = Field(default=1, ge=1, le=20)
    adults: int = Field(default=2, ge=1, le=40)
    children: int = Field(default=0, ge=0, le=20)
    currency: str = Field(default="PKR", min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self


class HotelSearchResponse(BaseModel):
    search_id: str
    status: str
    offers: list[dict] = Field(default_factory=list)
    vendors_queried: list[str] = Field(default_factory=list)
    vendors_succeeded: list[str] = Field(default_factory=list)
    vendors_failed: list[dict] = Field(default_factory=list)


@router.post("/search", response_model=HotelSearchResponse)
async def search_hotels(request: HotelSearchRequest) -> HotelSearchResponse:
    """Hotel search contract; supplier adapters will populate offers."""
    from uuid import uuid4

    return HotelSearchResponse(
        search_id=str(uuid4()),
        status="unavailable",
        offers=[],
        vendors_queried=[],
        vendors_succeeded=[],
        vendors_failed=[],
    )
