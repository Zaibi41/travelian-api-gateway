"""Public website routes: no login. Guests search, book and pay; what they get is decided here, never by the page.

A booking is opened with its reference plus the private token from the booking answer (header X-Booking-Token).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.schemas.flights import FlightSearchRequest
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


class CalendarQuery(BaseModel):
    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    adults: int = Field(1, ge=1, le=9)

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def upper(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


@router.post("/group-ticketing/calendar")
async def group_calendar(body: CalendarQuery, request: Request):
    """Every day for the sector: the lowest real selling price, or no availability. Never an invented price."""
    from datetime import date as _date

    from app.services import group_calendar as calendar

    try:
        service.check_rate(service.SEARCH_LIMIT, _ip(request))
        groups = await service.cached_inventory()
    except service.PublicError as error:
        return _error(error)
    return calendar.build(groups, body.origin, body.destination, body.adults, _date.today()).model_dump(mode="json", exclude_none=True)


class FlightCalendarQuery(BaseModel):
    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    adults: int = Field(1, ge=1, le=9)
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def upper(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


@router.post("/flights/calendar", response_model=service.FlightCalendar, response_model_exclude_none=True)
async def flight_calendar(body: FlightCalendarQuery, request: Request):
    """Lowest real fare per date (one-way, adults). Unknown dates have no price; a few are checked in the background."""
    try:
        service.check_rate(service.SEARCH_LIMIT, _ip(request))
    except service.PublicError as error:
        return _error(error)
    return await service.flight_calendar(body.origin, body.destination, body.adults, body.cabin)


@router.post("/flights/search", responses={429: {"description": "Too many searches."}})
async def search_flights(body: FlightSearchRequest, request: Request):
    """Live flights (all airlines Travelian sells), selling price only. Never supplier names, cost or supplier ids."""
    try:
        service.check_rate(service.SEARCH_LIMIT, _ip(request))
        view = await service.search_flights(body)
    except service.PublicError as error:
        return _error(error)
    return view.model_dump(mode="json", exclude_none=True, exclude={"providers"})


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


@router.get("/payment-methods", response_model=service.PaymentMethodsView, response_model_exclude_none=True)
async def payment_methods():
    """What checkout can offer now; bank-transfer accounts (full number, IBAN, QR image) and the site logo."""
    return await service.payment_methods()


class SiteConfig(BaseModel):
    umrah_enabled: bool = Field(description="Show Umrah anywhere on the site (D39). Off: every Umrah entry point and URL is hidden.")


@router.get("/site-config", response_model=SiteConfig)
async def site_config() -> SiteConfig:
    """Switches the website reads at load time, so they change with a server setting and no rebuild."""
    from app.core.config import settings

    return SiteConfig(umrah_enabled=settings.umrah_enabled)


@router.post("/bookings/{reference}/payments/jazzcash", include_in_schema=False)
async def start_jazzcash(reference: str):
    """JazzCash (scaffold, D31): not reachable while JAZZCASH_ENABLED is off."""
    from app.vendors.payments import jazzcash

    if not jazzcash.enabled():
        return JSONResponse({"detail": "JazzCash isn't available yet.", "code": "method_unavailable"}, status_code=404)
    return JSONResponse({"detail": "JazzCash checkout is scaffolded and not yet confirmed with JazzCash.", "code": "not_ready"}, status_code=503)


@router.api_route("/payments/jazzcash/notify", methods=["GET", "POST"], include_in_schema=False)
async def jazzcash_notify(request: Request):
    """JazzCash callback (scaffold): never records a payment until the flow is confirmed against JazzCash's guide."""
    from app.vendors.payments import jazzcash

    params = await _params(request)
    return JSONResponse({"status": "ignored", "verified": jazzcash.verify(params) if jazzcash.enabled() else False})


class LookupRequest(BaseModel):
    reference: str = Field(min_length=6, max_length=64)
    contact: str = Field(min_length=5, max_length=254, description="The email or phone number used to book.")


@router.post("/bookings/lookup", response_model=service.LookupView, response_model_exclude_none=True,
             responses={404: {"description": "No booking with that reference and contact."}, 429: {"description": "Too many lookups."}})
async def lookup_booking(body: LookupRequest, request: Request):
    """Find a booking without logging in: reference + the email or phone used to book. Read-only (D37)."""
    try:
        service.check_rate(service.LOOKUP_LIMIT, _ip(request))
        return await service.lookup(body.reference, body.contact)
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
