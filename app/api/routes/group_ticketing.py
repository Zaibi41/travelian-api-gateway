from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.booking_security import require_booking_authorization
from app.core.field_visibility import BackOfficeUser, FieldVisibility, require_search_viewer
from app.services.group_ticketing_view import GroupInventoryView, GroupTicketView, inventory_view, ticket_view
from app.core.config import settings
from app.services.group_ticketing import (
    GroupInventoryUnavailable,
    fetch_all_group_inventory,
    filter_group_inventory,
    group_ticket_id,
)
from app.schemas.flights import VendorIssue, VendorResult
from app.services.vendor_fanout import gather_vendor_calls
from app.vendors.group_ticketing.dhillu_sahi.auth import DhilluSahiAuthClient, DhilluSahiAuthError

# Sign-in required (internal or agency role; customers get 403). What each caller
# receives is decided in app.services.group_ticketing_view (D23).
router = APIRouter(
    prefix="/group-ticketing",
    tags=["group ticketing"],
    responses={401: {"description": "Not signed in."}, 403: {"description": "Role may not search."}},
)
Viewer = Annotated[BackOfficeUser, Depends(require_search_viewer)]
VENDOR_NAME = "dhillu_sahi"


class GroupQuoteRequest(BaseModel):
    origin: str = Field(min_length=3, max_length=3, examples=["KHI"])
    destination: str = Field(min_length=3, max_length=3, examples=["DXB"])
    departure_date: date
    return_date: date | None = None
    passenger_count: int = Field(ge=10, le=500)
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    currency: str = Field(default="PKR", min_length=3, max_length=3)
    contact_email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

    @field_validator("origin", "destination", "currency", mode="before")
    @classmethod
    def uppercase_codes(cls, value: str) -> str:
        return value.upper() if isinstance(value, str) else value


class GroupQuoteAcknowledgement(BaseModel):
    request_id: str
    status: Literal["received"]
    message: str


class GroupSearchRequest(BaseModel):
    origin: str = Field(min_length=3, max_length=3, examples=["LHE"])
    destination: str = Field(min_length=3, max_length=3, examples=["RUH"])
    departure_date: date
    adults: int = Field(default=1, ge=1, le=9)

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def normalize_airport_codes(cls, value: str) -> str:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def origin_and_destination_must_differ(self):
        if self.origin == self.destination:
            raise ValueError("origin and destination must be different")
        return self


# Supplier PNRs are opaque identifiers and can include punctuation. Keep them
# unchanged, bounded, and free of whitespace/control characters.
GroupPNR = Annotated[
    str, Field(strict=True, min_length=1, max_length=32, pattern=r"^[!-~]+$")
]


class GroupAvailabilityRequest(BaseModel):
    pnr: GroupPNR
    adult: int | None = Field(default=None, ge=0, le=100)
    child: int | None = Field(default=None, ge=0, le=100)
    passenger_types: list[Literal["adult", "child", "infant"]] | None = Field(
        default=None, min_length=1, max_length=100
    )
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def resolve_passenger_counts(self):
        if self.passenger_types is not None:
            if self.adult is not None or self.child is not None:
                raise ValueError("Use passenger_types or adult/child counts, not both")
            self.adult = self.passenger_types.count("adult")
            self.child = sum(kind in ("child", "infant") for kind in self.passenger_types)
        elif self.adult is None or self.child is None:
            raise ValueError("Both adult and child counts are required")
        if self.adult + self.child == 0:
            raise ValueError("At least one passenger is required")
        return self


class GroupAvailabilityResponse(BaseModel):
    request_id: str
    status: Literal["complete", "unavailable"]
    available: bool | None
    vendors_queried: list[str] | None = None
    vendors_succeeded: list[str] | None = None
    vendors_failed: list[VendorIssue] | None = None
    vendor_results: list[VendorResult] | None = None


class GroupBookingRequest(BaseModel):
    pnr: GroupPNR
    agent: int | None = Field(default=None, gt=0, deprecated=True)
    updated_at: str = Field(min_length=1, max_length=64)
    remarks: str = Field(default="", max_length=2000)
    titles: list[Literal["MR", "MRS", "MS", "MSTR", "MISS"]] = Field(min_length=1, max_length=100)
    firstname: list[str] = Field(min_length=1, max_length=100)
    lastname: list[str] = Field(min_length=1, max_length=100)
    dob: list[date] = Field(min_length=1, max_length=100)
    passport_no: list[str] = Field(min_length=1, max_length=100)
    passport_expiry_date: list[date] = Field(min_length=1, max_length=100)
    type: list[Literal["adult", "child", "infant"]] = Field(min_length=1, max_length=100)

    @field_validator("updated_at")
    @classmethod
    def timestamp_must_include_timezone(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("updated_at must include a timezone")
        return value

    @field_validator("firstname", "lastname", "passport_no")
    @classmethod
    def passenger_values_must_not_be_blank(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("passenger fields cannot contain blank values")
        return values

    @model_validator(mode="after")
    def passenger_arrays_must_align(self):
        lengths = {
            len(self.titles), len(self.firstname), len(self.lastname), len(self.dob),
            len(self.passport_no), len(self.passport_expiry_date), len(self.type),
        }
        if len(lengths) != 1:
            raise ValueError("all passenger arrays must have the same number of entries")
        return self


class GroupBookingResponse(BaseModel):
    request_id: str
    status: Literal["complete", "unavailable"]
    booking_id: int | None
    vendors_queried: list[str]
    vendors_succeeded: list[str]
    vendors_failed: list[VendorIssue]
    vendor_results: list[VendorResult]


class BookingPassenger(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | None = None
    title: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    dob: date | None = None
    passport_no: str | None = None
    passport_expiry_date: date | None = None
    type: str | None = None
    passport_img: str | None = None
    visa_copy: str | None = None


class BookingRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    agent: str | int | None = None
    pnr: str | None = None
    booking_date: str | None = None
    confirmed: str | int | None = None
    added_by: dict[str, Any] | None = None
    edit_by: dict[str, Any] | None = None
    cancel_by: dict[str, Any] | None = None
    total_amount: str | int | float | None = None
    final_total: str | int | float | None = None
    received_amount: str | int | float | None = None
    remaining_amount: str | int | float | None = None
    cancel_status: str | int | None = None
    cancel_at: str | None = None
    pdf_download_link: str | None = None
    pdf_view_link: str | None = None
    booking_sub: list[BookingPassenger] = Field(default_factory=list)
    payments: list[dict[str, Any]] = Field(default_factory=list)


class BookingGroupDetails(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | None = None
    pnr: str | None = None
    price: str | None = None
    airline: dict[str, Any] | None = None
    subpart: list[dict[str, Any]] = Field(default_factory=list)


class BookingDetailsData(BaseModel):
    booking: BookingRecord
    group: BookingGroupDetails


class BookingDetailsResponse(BaseModel):
    request_id: str
    status: Literal["complete", "unavailable"]
    details: BookingDetailsData | None
    message: str | None = None
    vendors_queried: list[str]
    vendors_succeeded: list[str]
    vendors_failed: list[VendorIssue]
    vendor_results: list[VendorResult]


@router.post("/flight-quotes", response_model=GroupQuoteAcknowledgement, status_code=202, dependencies=[Depends(require_search_viewer)])
async def request_group_quote(request: GroupQuoteRequest) -> GroupQuoteAcknowledgement:
    """Accept a group quote request; sourcing workflow is not wired yet."""
    return GroupQuoteAcknowledgement(
        request_id=str(uuid4()),
        status="received",
        message="Group quote request received. A ticketing agent will follow up.",
    )


async def inventory_for(visibility: FieldVisibility) -> GroupInventoryView:
    """All group inventory from configured suppliers, as `visibility` may see it."""
    try:
        groups = await fetch_all_group_inventory()
    except GroupInventoryUnavailable as exc:
        raise HTTPException(status_code=503, detail="Group inventory is temporarily unavailable") from exc
    return inventory_view(groups, visibility)


async def search_for(request: "GroupSearchRequest", visibility: FieldVisibility) -> GroupInventoryView:
    """Combined one-way inventory matching the search, as `visibility` may see it."""
    try:
        groups = await fetch_all_group_inventory()
    except GroupInventoryUnavailable as exc:
        raise HTTPException(status_code=503, detail="Group inventory is temporarily unavailable") from exc
    matches = filter_group_inventory(
        groups,
        origin=request.origin,
        destination=request.destination,
        departure_date=request.departure_date,
        adults=request.adults,
    )
    return inventory_view(matches, visibility)


@router.get("/inventory", response_model=GroupInventoryView, response_model_exclude_none=True)
async def fetch_group_inventory(viewer: Viewer) -> GroupInventoryView:
    """Fetch and combine all group inventory from configured suppliers."""
    return await inventory_for(viewer.visibility)


@router.post("/search", response_model=GroupInventoryView, response_model_exclude_none=True)
async def search_group_inventory(request: GroupSearchRequest, viewer: Viewer) -> GroupInventoryView:
    """Search combined one-way inventory; adult count defaults to one."""
    return await search_for(request, viewer.visibility)


@router.get("/tickets/{ticket_id}", response_model=GroupTicketView, response_model_exclude_none=True)
async def fetch_group_ticket(ticket_id: UUID, viewer: Viewer) -> GroupTicketView:
    """Select one record from refreshed inventory; no invented supplier endpoint."""
    try:
        groups = await fetch_all_group_inventory()
    except GroupInventoryUnavailable as exc:
        raise HTTPException(status_code=503, detail="Group inventory is temporarily unavailable") from exc
    matches = [
        ticket for group in groups for ticket in group.tickets
        if group_ticket_id(ticket) == str(ticket_id)
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="Ticket is not available in current inventory")
    if len(matches) != 1:
        raise HTTPException(status_code=409, detail="Ticket selection is ambiguous; refresh inventory")
    return ticket_view(matches[0], viewer.visibility)


@router.post("/check-availability", response_model=GroupAvailabilityResponse, response_model_exclude_unset=True)
async def check_group_availability(request: GroupAvailabilityRequest, viewer: Viewer) -> GroupAvailabilityResponse:
    if not _credentials_configured():
        issues, results = _not_configured_outcome()
        return GroupAvailabilityResponse(
            request_id=str(uuid4()), status="unavailable", available=None,
            **_availability_metadata(viewer, [], issues, results),
        )
    values, issues, results = await gather_vendor_calls(
        [(VENDOR_NAME, lambda: _check_dhillu_sahi_availability(request))],
        empty_response_code="invalid_response",
    )
    succeeded = [result.vendor for result in results if result.status == "success"]
    return GroupAvailabilityResponse(
        request_id=str(uuid4()), status="complete" if succeeded else "unavailable",
        available=values.get(VENDOR_NAME),
        **_availability_metadata(viewer, succeeded, issues, results),
    )


def _availability_metadata(viewer: BackOfficeUser, succeeded, issues, results) -> dict:
    if not viewer.visibility.supplier_names:
        return {}
    return {"vendors_queried": [VENDOR_NAME], "vendors_succeeded": succeeded,
            "vendors_failed": issues, "vendor_results": results}


@router.post(
    "/bookings",
    response_model=GroupBookingResponse,
    dependencies=[Depends(require_booking_authorization)],
    responses={503: {"description": "Booking authorization is not configured"}},
)
async def create_group_booking(request: GroupBookingRequest) -> GroupBookingResponse:
    if not _credentials_configured():
        issues, results = _not_configured_outcome()
        return GroupBookingResponse(
            request_id=str(uuid4()), status="unavailable", booking_id=None,
            vendors_queried=[VENDOR_NAME], vendors_succeeded=[],
            vendors_failed=issues, vendor_results=results,
        )
    values, issues, results = await gather_vendor_calls(
        [(VENDOR_NAME, lambda: _create_dhillu_sahi_booking(request))],
        empty_response_code="invalid_response",
    )
    booking_id = values.get(VENDOR_NAME)
    succeeded = [result.vendor for result in results if result.status == "success"]
    return GroupBookingResponse(
        request_id=str(uuid4()), status="complete" if booking_id is not None else "unavailable",
        booking_id=booking_id, vendors_queried=[VENDOR_NAME],
        vendors_succeeded=succeeded, vendors_failed=issues, vendor_results=results,
    )


@router.post(
    "/booking-details",
    response_model=BookingDetailsResponse,
    dependencies=[Depends(require_booking_authorization)],
    responses={503: {"description": "Booking authorization is not configured"}},
)
async def fetch_booking_details(request: "BookingDetailsRequest") -> BookingDetailsResponse:
    if not _credentials_configured():
        issues, results = _not_configured_outcome()
        return BookingDetailsResponse(
            request_id=str(uuid4()), status="unavailable", details=None,
            message=None, vendors_queried=[VENDOR_NAME], vendors_succeeded=[],
            vendors_failed=issues, vendor_results=results,
        )
    values, issues, results = await gather_vendor_calls(
        [(VENDOR_NAME, lambda: _fetch_dhillu_sahi_booking(request.booking_id))],
        empty_response_code="invalid_response",
    )
    succeeded = [result.vendor for result in results if result.status == "success"]
    supplier_result = values.get(VENDOR_NAME)
    return BookingDetailsResponse(
        request_id=str(uuid4()), status="complete" if succeeded else "unavailable",
        details=supplier_result, message=supplier_result.get("message") if supplier_result else None,
        vendors_queried=[VENDOR_NAME],
        vendors_succeeded=succeeded, vendors_failed=issues, vendor_results=results,
    )


class BookingDetailsRequest(BaseModel):
    booking_id: int = Field(gt=0)


def _credentials_configured() -> bool:
    password = settings.dhillu_sahi_password
    return bool(settings.dhillu_sahi_email and password and password.get_secret_value())


def _not_configured_outcome() -> tuple[list[VendorIssue], list[VendorResult]]:
    return (
        [VendorIssue(vendor=VENDOR_NAME, code="not_configured", message="Supplier credentials are not configured.")],
        [VendorResult(vendor=VENDOR_NAME, status="failed", duration_ms=0, error_code="not_configured")],
    )


async def _authenticated_dhillu_sahi_client() -> tuple[DhilluSahiAuthClient, str]:
    client = DhilluSahiAuthClient(
        settings.dhillu_sahi_base_url,
        timeout_seconds=settings.dhillu_sahi_timeout_seconds,
    )
    password = settings.dhillu_sahi_password
    login = await client.login(
        settings.dhillu_sahi_email or "",
        password.get_secret_value() if password else "",
    )
    return client, login["token"]


async def _check_dhillu_sahi_availability(request: GroupAvailabilityRequest) -> bool:
    client, token = await _authenticated_dhillu_sahi_client()
    return await client.check_availability(
        token, pnr=request.pnr, adult=request.adult, child=request.child, price=float(request.price)
    )


async def _create_dhillu_sahi_booking(request: GroupBookingRequest) -> int:
    client, token = await _authenticated_dhillu_sahi_client()
    if client.agent_id is None:
        raise DhilluSahiAuthError("Supplier login did not establish an agent", error_code="agent_identity_unavailable")
    body = request.model_dump(mode="json")
    if body.get("agent") is not None and body["agent"] != client.agent_id:
        raise DhilluSahiAuthError("Requested agent does not match supplier login", error_code="agent_identity_mismatch")
    body["agent"] = client.agent_id
    return await client.add_booking(token, body)


async def _fetch_dhillu_sahi_booking(booking_id: int) -> BookingDetailsData:
    client, token = await _authenticated_dhillu_sahi_client()
    return await client.fetch_booking_details(token, booking_id)
