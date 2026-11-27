"""Public website routes: no login. Guests search, book and pay; what they get is decided here, never by the page.

A booking is opened with its reference plus the private token from the booking answer (header X-Booking-Token).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, Header, Path, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from app.schemas.flights import MAX_LEGS, FlightSearchRequest
from app.schemas.live_flights import RequiredDocument
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
        service.check_rate(service.CALENDAR_LIMIT, _ip(request))
        groups = await service.cached_inventory()
    except service.PublicError as error:
        return _error(error)
    return calendar.build(groups, body.origin, body.destination, body.adults, _date.today()).model_dump(mode="json", exclude_none=True)


class FlightCalendarQuery(BaseModel):
    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    adults: int = Field(1, ge=1, le=9)
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    start: date | None = Field(None, description="First day (default today). The gateway checks unknown days from here.")

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def upper(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


@router.post("/flights/calendar", response_model=service.FlightCalendar, response_model_exclude_none=True)
async def flight_calendar(body: FlightCalendarQuery, request: Request):
    """Lowest real fare per date (one-way, adults). Unknown dates have no price; a few are checked in the background.
    One call per page (date picker or the results page's date strip); the page asks again while `pending`."""
    try:
        service.check_rate(service.CALENDAR_LIMIT, _ip(request))
    except service.PublicError as error:
        return _error(error)
    return await service.flight_calendar(body.origin, body.destination, body.adults, body.cabin, body.start)


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


@router.post("/flights/requests", response_model=service.PublicBookingCreated, status_code=201,
             responses={404: {"description": "The search expired."}, 409: {"description": "The fare is gone or changed."},
                        422: {"description": "Check the details."}, 429: {"description": "Too many bookings."},
                        502: {"description": "The fare couldn't be held; nothing was charged."}})
async def request_flight(body: service.PublicFlightRequest, request: Request):
    """A guest's flight booking, repriced by the gateway. PUBLIC_FLIGHT_BOOKING_ENABLED off (or a restricted airline, or
    booking off for the fare's provider): saved as a request for Travelian's team, never held, ticketed or charged, no PNR.
    On: held at the supplier, then paid by `payment_method`: card through PayFast (issued automatically only after
    PayFast's verified payment, when ticketing is on, D48), or bank transfer / JazzCash (staff verify and issue, D49)."""
    try:
        service.check_rate(service.BOOK_LIMIT, _ip(request))
        return await service.request_flight(body)
    except service.PublicError as error:
        return _error(error)


class FlightPaymentQuery(BaseModel):
    search_id: str = Field(min_length=8, max_length=64)
    fare_ids: list[str] = Field(min_length=1, max_length=MAX_LEGS)


@router.post("/flights/payment-options", response_model=service.FlightPaymentOptions,
             responses={404: {"description": "The search expired."}, 429: {"description": "Too many requests."}})
async def flight_payment_options(body: FlightPaymentQuery, request: Request):
    """Which payment methods checkout offers for the chosen fares (the booking route applies the same rules)."""
    try:
        service.check_rate(service.CALENDAR_LIMIT, _ip(request))
        return await service.flight_payment_options(body.search_id, body.fare_ids)
    except service.PublicError as error:
        return _error(error)


class FlightDocuments(BaseModel):
    documents: list[RequiredDocument] = Field(description="Travelers whose airline asks for that document on this fare. Empty: no "
                                                          "specific document known (the booking route still checks).")


@router.post("/flights/documents", response_model=FlightDocuments,
             responses={404: {"description": "The search expired."}, 429: {"description": "Too many requests."},
                        502: {"description": "The airline's rules couldn't be read; the booking route still checks them."}})
async def flight_documents(body: FlightPaymentQuery, request: Request):
    """Which travelers must give a CNIC (or a passport) for the chosen fares, so checkout asks for it before submitting."""
    from app.services import live_flights

    try:
        service.check_rate(service.DOCUMENTS_LIMIT, _ip(request))
        return FlightDocuments(documents=await live_flights.document_requirements(body.search_id, body.fare_ids, service.WEBSITE_SEARCH_VIEWER))
    except service.PublicError as error:
        return _error(error)
    except live_flights.RepriceError as error:
        return JSONResponse({"detail": error.message, "code": error.code}, status_code=error.status)


Token = Annotated[str, Header(alias="X-Booking-Token", min_length=8, max_length=200)]


@router.get("/booking-links/{token}", response_model=service.LookupView, response_model_exclude_none=True,
            responses={404: {"description": "The link is unknown or has expired."}, 429: {"description": "Too many requests."}})
async def booking_by_link(token: Annotated[str, Path(min_length=8, max_length=100)], request: Request):
    """The booking a customer email's link opens: read-only (no payment from here), one booking, no login. Links expire at
    the later of 30 days and 2 days after the last travel date."""
    from app.services import customer_emails

    try:
        service.check_rate(service.SEARCH_LIMIT, _ip(request))  # tokens can't be guessed; this only stops floods
    except service.PublicError as error:
        return _error(error)
    reference = await run_in_threadpool(customer_emails.booking_for_link, token)
    if reference is None:
        return _error(service.PublicError(404, "This link has expired or isn't valid. Find your booking with its reference and your email or phone.", "link_expired"))
    try:
        return await service.lookup_by_link(reference)
    except service.PublicError as error:
        return _error(error)


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


@router.get("/payment-qr/{account_id}", response_class=Response,
            responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}, "description": "The QR image as a download."},
                       404: {"description": "No such account QR at checkout."}, 502: {"description": "The image couldn't be read."}})
async def payment_qr(request: Request, account_id: Annotated[str, Path(max_length=64)]):
    """Download QR: the checkout account's QR image as a file (Content-Disposition: attachment), so it saves in every
    browser, including iPhone Safari and Android; the S3 image itself can't be downloaded cross-site."""
    try:
        service.check_rate(service.QR_LIMIT, _ip(request))
        content, kind, name = await service.payment_qr(account_id)
    except service.PublicError as error:
        return _error(error)
    return Response(content, media_type=kind, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                       "Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})


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
            elif "multipart" in content_type:
                form = await request.form()
                params.update({str(k): str(v) for k, v in form.items()})
            else:
                # PayFast posts application/x-www-form-urlencoded. Parsed here: request.form() needs python-multipart,
                # which isn't installed, so a posted notification used to be read as empty and ignored.
                from urllib.parse import parse_qsl

                params.update(dict(parse_qsl((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)))
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
    result: str = Field(description="paid, failed, already_paid, already_failed, amount_mismatch, unknown_basket, not_verified, "
                                    "or not_paid (Bank Alfalah: the order isn't paid, yet).")


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


@router.post("/bookings/{reference}/payments/alfalah", response_model=service.CheckoutView)
async def start_alfalah(reference: str, token: Token, request: Request):
    """The Bank Alfalah (Alfa Payment Gateway) checkout form for the amount due. The browser posts `fields` to `action`."""
    try:
        service.check_rate(service.PAY_LIMIT, _ip(request))
        return await service.start_alfalah_payment(reference, token)
    except service.PublicError as error:
        return _error(error)


@router.api_route("/payments/alfalah/notify", methods=["GET", "POST"], include_in_schema=False)
async def alfalah_notify(request: Request):
    """Bank Alfalah's listener call (url=…/OrderStatus/{merchant}/{store}/{order}). Only the order id is taken from it; the
    gateway then asks APG itself for that order's status."""
    outcome = await service.alfalah_result(await _params(request), source="notification")
    return JSONResponse({"status": "ok" if outcome else "ignored"})


@router.post("/payments/alfalah/return", response_model=ReturnResult)
async def alfalah_return(request: Request):
    """The website forwards what APG sent the customer back with (the order id "O", in the query or the path). The gateway
    asks APG for that order's status; nothing the browser sends is trusted as a payment."""
    try:
        service.check_rate(service.PAY_LIMIT, _ip(request))
    except service.PublicError as error:
        return _error(error)
    outcome = await service.alfalah_result(await _params(request), source="return")
    if outcome is None:
        return ReturnResult(result="not_verified")
    return ReturnResult(reference=outcome.reference or None, result=outcome.status)


class CardFeeQuery(BaseModel):
    amount_minor: int = Field(gt=0, le=10_000_000_000, description="The amount to pay (paisa), as shown by the gateway's own price.")
    product: Literal["flight", "group", "hotel", "umrah"] = "flight"


@router.post("/card-fee", response_model=service.CardFeeView, responses={204: {"description": "No fee applies."}})
async def card_fee_quote(body: CardFeeQuery):
    """The card / 1Link fee shown before payment ("Card fee (3%): Rs X · Total Rs Y"). Display only: the amount PayFast is
    charged is worked out again by the gateway from the booking when payment starts (travelian#74)."""
    view = await run_in_threadpool(service.card_fee_for, body.amount_minor, body.product, "PKR")
    return view if view is not None else Response(status_code=204)


# ---------- Contact page (gateway#114) ----------

_PHONE = r"^\+?[0-9][0-9 ()-]{6,19}$"


class ContactMessage(BaseModel):
    """A message from the Contact page. Name and message are required, and an email or a mobile number to reply to.
    `website` is a hidden field people never fill in: when it has a value the message is treated as spam (accepted, not kept)."""

    name: str = Field(min_length=2, max_length=80)
    email: str | None = Field(None, max_length=120, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    phone: str | None = Field(None, max_length=20, pattern=_PHONE)
    message: str = Field(min_length=10, max_length=2000)
    website: str | None = Field(None, max_length=200, description="Leave empty (spam trap).")

    @field_validator("name", "message", "email", "phone", "website", mode="before")
    @classmethod
    def strip(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value


class ContactReceived(BaseModel):
    message: str


@router.post("/contact", response_model=ContactReceived, status_code=202,
             responses={422: {"description": "Missing or invalid fields."}, 429: {"description": "Too many messages."},
                        503: {"description": "It couldn't be sent; nothing was saved."}})
async def contact(body: ContactMessage, request: Request):
    """gateway#114: send a message to the Travelian team (stored in the admin under Notifications and emailed to the team
    when email is set up). At most 5 per hour from one address. Never logs the message."""
    from app.services import notifications

    if not body.email and not body.phone:
        return JSONResponse({"detail": "Add an email address or a mobile number so we can reply.", "code": "contact_required"}, status_code=422)
    try:
        service.check_rate(service.CONTACT_LIMIT, _ip(request))
    except service.PublicError as error:
        return _error(error)
    if not body.website:  # a filled spam trap is answered the same way, and nothing is kept
        try:
            await notifications.contact_message(name=body.name, email=body.email, phone=body.phone, message=body.message)
        except Exception:  # noqa: BLE001 - stored nowhere: the sender is told to try again or use WhatsApp
            return JSONResponse({"detail": "Your message couldn't be sent right now. Please try again, or message us on WhatsApp.",
                                 "code": "not_sent"}, status_code=503)
    return ContactReceived(message="Thanks, your message has been sent. Our team will reply within working hours (Mon–Sat, 9 AM – 6 PM PKT).")
