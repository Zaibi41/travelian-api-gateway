"""Customer payment slips and the chosen receiving account (gateway#56, travelian#76). No login.

Access: the booking's private token (X-Booking-Token, checkout) or, from My Bookings, the email or phone the booking was
made with (`contact`), the same check as the booking lookup. Rate-limited per IP. The slip file goes straight to private
S3 with a signed POST; the gateway then checks its real type and size before saving it. Staff are alerted.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.db import payment_slips, public_bookings
from app.services import payment_proof
from app.services import public_booking as service

router = APIRouter(prefix="/public/bookings/{reference}", tags=["public website"])
SLIP_LIMIT = service.RateLimiter(20, 3600)
OptionalToken = Annotated[str | None, Header(alias="X-Booking-Token", max_length=200)]


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse({"detail": message, "code": code}, status_code=status)


class Access(BaseModel):
    contact: str | None = Field(None, max_length=120, description="My Bookings: the email or phone the booking was made with.")


def _allowed(reference: str, token: str | None, contact: str | None) -> bool:
    if token and public_bookings.guest_can_open(reference, token):
        return True
    return bool(contact and contact.strip() and public_bookings.lookup(reference, contact) is not None)


async def _check(reference: str, token: str | None, contact: str | None, request: Request) -> JSONResponse | None:
    try:
        service.check_rate(SLIP_LIMIT, _ip(request))
    except service.PublicError as error:
        return _error(error.status, error.message, error.code)
    if not await run_in_threadpool(_allowed, reference.strip().upper(), token, contact):
        return _error(404, "Booking not found. Check the reference and the email or phone you booked with.", "not_found")
    return None


class SlipUploadRequest(Access):
    content_type: Literal["image/jpeg", "image/png", "image/webp", "application/pdf"]


class SlipUploadView(BaseModel):
    url: str
    fields: dict[str, str]
    asset_key: str
    max_bytes: int


@router.post("/payment-slips/upload", response_model=SlipUploadView, responses={404: {}, 422: {}, 429: {}, 503: {}})
async def sign_slip_upload(reference: str, body: SlipUploadRequest, request: Request, token: OptionalToken = None):
    """A one-time signed upload (10 minutes) for the slip file: JPG, PNG, WebP or PDF, at most 5 MB, private."""
    if (refused := await _check(reference, token, body.contact, request)) is not None:
        return refused
    try:
        signed = await run_in_threadpool(payment_proof.sign_upload, body.content_type)
    except payment_proof.ProofError as error:
        return _error(error.status, error.message, error.code)
    return SlipUploadView(url=signed.url, fields=signed.fields, asset_key=signed.asset_key, max_bytes=signed.max_bytes)


class SlipSubmitRequest(Access):
    method: Literal["bank_transfer", "jazzcash"]
    account_id: str | None = Field(None, max_length=36)
    amount_minor: int = Field(gt=0)
    payer_reference: str | None = Field(None, max_length=64)
    asset_key: str = Field(max_length=300)
    via: Literal["checkout", "my_bookings"] = "checkout"


class SlipSubmitted(BaseModel):
    status: Literal["awaiting_check"]
    message: str


@router.post("/payment-slips", response_model=SlipSubmitted, status_code=201, responses={404: {}, 409: {}, 422: {}, 429: {}})
async def submit_slip(reference: str, body: SlipSubmitRequest, request: Request, token: OptionalToken = None):
    """Save the uploaded slip as "awaiting check" and alert staff. The payment counts only after staff confirm it."""
    if (refused := await _check(reference, token, body.contact, request)) is not None:
        return refused
    reference = reference.strip().upper()
    try:
        kind, size = await run_in_threadpool(payment_proof.inspect, body.asset_key)
        slip = await run_in_threadpool(lambda: payment_slips.submit(
            reference, method=body.method, account_id=body.account_id, amount_minor=body.amount_minor, payer_reference=body.payer_reference,
            asset_key=body.asset_key, content_type=kind, size_bytes=size, via=body.via))
    except payment_proof.ProofError as error:
        return _error(error.status, error.message, error.code)
    except payment_slips.SlipRefused as error:
        return _error(error.status, error.message, error.code)
    from app.services import notifications

    method = "Bank transfer" if slip.method == "bank_transfer" else "JazzCash"
    await notifications.booking_event("payment_slip_uploaded", reference=reference, passenger=None, route=None, fare_minor=slip.amount_minor,
                                      currency=slip.currency, pnr=None, hold_expires_at=None,
                                      note=f"{method} slip into {slip.account_label or 'an account not chosen'}. Check it in Finance > Payments.")
    return SlipSubmitted(status="awaiting_check", message="Slip received. Travelian will check the payment and confirm it; "
                                                          "your booking is not paid until then.")


class ChooseAccountRequest(Access):
    account_id: str = Field(max_length=36)


@router.post("/chosen-account", status_code=204, responses={404: {}, 422: {}, 429: {}})
async def choose_account(reference: str, body: ChooseAccountRequest, request: Request, token: OptionalToken = None):
    """Remember which receiving account the customer chose (staff see it on the booking's Payments tab)."""
    if (refused := await _check(reference, token, body.contact, request)) is not None:
        return refused
    try:
        await run_in_threadpool(payment_slips.choose_account, reference.strip().upper(), body.account_id)
    except payment_slips.SlipRefused as error:
        return _error(error.status, error.message, error.code)
    return Response(status_code=204)
