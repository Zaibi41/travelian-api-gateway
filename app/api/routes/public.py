"""Public website routes: no login. Guests search, book and pay; what they get is decided here, never by the page.

A booking is opened with its reference plus the private token from the booking answer (header X-Booking-Token).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.services import public_booking as service

router = APIRouter(prefix="/public", tags=["public website"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _error(error: service.PublicError) -> JSONResponse:
    return JSONResponse({"detail": error.message, "code": error.code}, status_code=error.status)


class PublicGroupSearch(BaseModel):
    origin: str | None = Field(None, min_length=3, max_length=3)
    destination: str | None = Field(None, min_length=3, max_length=3)
    departure_date: date | None = None
    adults: int = Field(1, ge=1, le=9)

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def upper(cls, value):
        return value.strip().upper() or None if isinstance(value, str) else value


@router.post("/group-ticketing/search",
             responses={429: {"description": "Too many searches."}, 503: {"description": "Suppliers unavailable."}})
async def search_groups(body: PublicGroupSearch, request: Request):
    """Group fares with selling prices only. Leave all of origin, destination and date out for everything."""
    try:
        service.check_rate(service.SEARCH_LIMIT, _ip(request))
        return await service.search(body.origin, body.destination, body.departure_date, body.adults)
    except service.PublicError as error:
        return _error(error)


@router.post("/group-ticketing/bookings", response_model=service.PublicBookingCreated, status_code=201,
             responses={409: {"description": "Fare gone or seats changed."}, 422: {"description": "Check the details."},
                        429: {"description": "Too many bookings."}})
async def book_group(body: service.PublicGroupBookingRequest, request: Request):
    """Book as a guest. With live booking on, seats are held at the supplier; otherwise a request is saved for staff."""
    try:
        service.check_rate(service.BOOK_LIMIT, _ip(request))
        return await service.book_group(body)
    except service.PublicError as error:
        return _error(error)


Token = Annotated[str, Header(alias="X-Booking-Token", min_length=8, max_length=200)]


@router.get("/bookings/{reference}", response_model=service.PublicBookingView, response_model_exclude_none=True)
async def get_booking(reference: str, token: Token, request: Request):
    try:
        service.check_rate(service.PAY_LIMIT, _ip(request))
        return await service.open_booking(reference, token)
    except service.PublicError as error:
        return _error(error)


@router.post("/bookings/{reference}/payments/payfast", response_model=service.CheckoutView)
async def start_payfast(reference: str, token: Token, request: Request):
    """The PayFast checkout form for the amount due. The browser posts `fields` to `action`."""
    try:
        service.check_rate(service.PAY_LIMIT, _ip(request))
        return await service.start_payment(reference, token)
    except service.PublicError as error:
        return _error(error)


async def _params(request: Request) -> dict[str, str]:
    params = {k: v for k, v in request.query_params.items()}
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        try:
            if "json" in content_type:
                body = await request.json()
                if isinstance(body, dict):
                    params.update({str(k): str(v) for k, v in body.items() if v is not None})
            else:
                form = await request.form()
                params.update({str(k): str(v) for k, v in form.items()})
        except Exception:  # noqa: BLE001 - an unreadable body is treated as no parameters
            pass
    return params


@router.api_route("/payments/payfast/notify", methods=["GET", "POST"], include_in_schema=False)
async def payfast_notify(request: Request):
    """PayFast's server notification (CHECKOUT_URL). Applied only when its validation hash verifies."""
    outcome = await service.payfast_result(await _params(request), source="notification")
    return JSONResponse({"status": "ok" if outcome else "ignored"})


class ReturnResult(BaseModel):
    reference: str | None = None
    result: str = Field(description="paid, failed, already_paid, already_failed, amount_mismatch, unknown_basket or not_verified.")


@router.post("/payments/payfast/return", response_model=ReturnResult)
async def payfast_return(request: Request):
    """The website forwards the parameters PayFast sent the customer back with. Same verification as the notification."""
    try:
        service.check_rate(service.PAY_LIMIT, _ip(request))
    except service.PublicError as error:
        return _error(error)
    outcome = await service.payfast_result(await _params(request), source="return")
    if outcome is None:
        return ReturnResult(result="not_verified")
    return ReturnResult(reference=outcome.reference or None, result=outcome.status)
