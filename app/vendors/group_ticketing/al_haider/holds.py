"""Al Haider seat holds through create/booking (production API: bookings are real). Off unless
GROUP_AL_HAIDER_HOLD_ENABLED is true: Al Haider hasn't yet confirmed that an unpaid booking expires by itself.

Contract: docs/postman/Al Haider Travel.postman_collection.json, permanent Bearer token (AL_HAIDER_ACCESS_TOKEN).
- GET /api/user: the token's own account (read live 14 Nov 2026: {id, name, email, mobile_no, city, email_verified_at},
  not wrapped). Read before each booking for agency_info: agent_name and agency_name = name (the profile has no separate
  agent name), email = email, mobile = mobile_no. Incomplete profile: nothing is booked.
- POST /api/create/booking, JSON:
    {"group_id": <int>, "agency_info": {"group_id", "agent_name", "agency_name", "email", "mobile", "adults", "child": 0,
     "infant": 0, "agent_notes": null}, "booking_details": [{"type": "Adult", "surname", "given_name", "title" (MR, MRS, MS),
     "passport_no", "dob", "doe" (YYYY-MM-DD)}]}
  The collection documents the answer only as "status" and "booking_id" (no example). Assumed: {"status": true or
  "success", "booking_id": <int>}, also accepted under "data". Not verified live.
- GET /api/show/booking/{id}: the booking (documented schema): id, group_id, adults, fares, status (int),
  auto_cancel_after, sold_at, canceled_at, created_at, passengers[], booking_details[], created_by. Read after the hold.
Cancel and confirm calls exist in the collection ("Permission Based"); neither is used: nothing is released at our
expiry. create/booking is never retried: a timeout may have booked the seats.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.group_ticketing import GroupTicket
from app.schemas.live_flights import HoldContact, HoldPassenger
from app.services.group_ticketing_view import GroupMoney, supplier_cost
from app.vendors.group_ticketing.al_haider.inventory import AlHaiderError
from app.vendors.group_ticketing.al_haider.inventory import is_configured as listing_configured

logger = logging.getLogger(__name__)
NAME = "al_haider"
TITLES = {"MR": "MR", "MRS": "MRS", "MISS": "MS", "MS": "MS"}
#: Answers that clearly mean nothing was booked (validation, sold out, wrong group).
REFUSED_HTTP = {400, 404, 409, 410, 422}
#: Booking fields kept on our record from show/booking (traveler arrays and the creator's contact stay out).
DETAIL_KEYS = ("id", "group_id", "adults", "child", "infant", "fares", "status", "auto_cancel_after", "sold_at", "canceled_at", "created_at")


def is_configured() -> bool:
    return listing_configured()


def agency_from_profile(profile: Any) -> dict[str, str]:
    """agency_info fields from GET /api/user. Raises AlHaiderError (nothing is booked) when any is missing."""
    data = profile.get("data", profile) if isinstance(profile, dict) else None
    data = data if isinstance(data, dict) else {}
    name, email, mobile = (str(data.get(key) or "").strip() for key in ("name", "email", "mobile_no"))
    if not (name and email and mobile):
        raise AlHaiderError("Al Haider account profile has no name, email or mobile", error_code="profile_incomplete")
    return {"agent_name": name, "agency_name": name, "email": email, "mobile": mobile}


def booking_payload(ticket: GroupTicket, passengers: list[HoldPassenger], agency: dict[str, str]) -> dict[str, Any]:
    """The create/booking body: Travelian's agency (from its profile), adults only, one booking_details entry per traveler."""
    if not ticket.supplier_group_id or not ticket.supplier_group_id.isdigit():
        raise ValueError("the listing gave no group id for this fare")
    if any(passenger.type != "ADT" for passenger in passengers):
        raise ValueError("Al Haider holds are for adults only")
    if any(not passenger.document_number or passenger.date_of_birth is None or passenger.document_expiry is None
           or passenger.title not in TITLES for passenger in passengers):
        raise ValueError("every traveler needs a title, passport number, date of birth and passport expiry")
    group_id = int(ticket.supplier_group_id)
    return {
        "group_id": group_id,
        "agency_info": {
            "group_id": group_id, "agent_name": agency["agent_name"], "agency_name": agency["agency_name"],
            "email": agency["email"], "mobile": agency["mobile"],
            "adults": len(passengers), "child": 0, "infant": 0, "agent_notes": None,
        },
        "booking_details": [{
            "type": "Adult", "surname": passenger.last_name, "given_name": passenger.first_name, "title": TITLES[passenger.title],
            "passport_no": passenger.document_number, "dob": passenger.date_of_birth.isoformat(), "doe": passenger.document_expiry.isoformat(),
        } for passenger in passengers],
    }


def missing_traveler_details(passengers: list[HoldPassenger]) -> list[tuple[int, str]]:
    problems = []
    for index, passenger in enumerate(passengers):
        if passenger.id_type != "PSPT" or not passenger.document_number:
            problems.append((index, "document_number"))
        elif passenger.document_expiry is None:
            problems.append((index, "document_expiry"))
    return problems


def booking_id_of(payload: Any) -> int | None:
    """The new booking's id: top-level booking_id, or under data (booking_id or id)."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for value in (payload.get("booking_id"), data.get("booking_id"), data.get("id")):
        if type(value) is int and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)
    return None


def _accepted(payload: dict[str, Any]) -> bool | None:
    """True / False when the answer says so ("status" or "success", also the collection's misspelt "sucess"); None if silent."""
    for key in ("status", "success", "sucess"):
        if key in payload:
            value = payload[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().casefold() in ("success", "true", "ok", "1")
            if isinstance(value, int):
                return value == 1
    return None


def detail_summary(booking: dict[str, Any]) -> dict[str, Any]:
    return {key: booking.get(key) for key in DETAIL_KEYS if key in booking}


class AlHaiderHoldProvider:
    """GroupHoldProvider for Al Haider. `transport` lets tests answer the HTTP calls."""

    supports_issue = False
    issue_unavailable_reason = "Issuing is not available for this provider yet"
    can_release = False
    #: "Supplier hold ends by itself after the supplier's expiry" is shown only once Al Haider confirms the time.
    supplier_hold_notice = None

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = (settings.al_haider_base_url or "").rstrip("/")
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(settings.vendor_timeout_seconds, connect=min(settings.vendor_timeout_seconds, 3.0))
        return httpx.AsyncClient(timeout=timeout, transport=self.transport)

    def _headers(self) -> dict[str, str]:
        token = settings.al_haider_access_token.get_secret_value() if settings.al_haider_access_token else ""
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def check_passengers(self, passengers: list[HoldPassenger]) -> list[tuple[int, str]]:
        return missing_traveler_details(passengers)

    async def hold(self, ticket: GroupTicket, passengers: list[HoldPassenger], contact: HoldContact):
        from app.services.group_holds import HeldSeats, SupplierRefused  # late import: group_holds imports this module

        cost = supplier_cost(ticket)
        if cost is None:
            raise AlHaiderError("Al Haider fare has no price", error_code="no_price")
        async with self._client() as client:
            try:
                profile = await client.get(f"{self.base_url}/api/user", headers=self._headers())
                profile.raise_for_status()
                agency = agency_from_profile(profile.json())
            except AlHaiderError:
                raise
            except (httpx.HTTPError, ValueError) as exc:  # nothing booked yet
                raise AlHaiderError("Al Haider account profile could not be read", error_code="profile_unavailable") from exc
            body = booking_payload(ticket, passengers, agency)
            try:
                response = await client.post(f"{self.base_url}/api/create/booking", json=body, headers=self._headers())  # never retried
            except httpx.TimeoutException as exc:
                raise AlHaiderError("Al Haider booking timed out", error_code="upstream_timeout") from exc
            except httpx.HTTPError as exc:
                raise AlHaiderError("Al Haider booking request failed") from exc
            if response.status_code in REFUSED_HTTP:
                raise SupplierRefused(f"upstream_http_{response.status_code}")
            if response.status_code >= 400:
                raise AlHaiderError(f"Al Haider booking returned HTTP {response.status_code}", error_code=f"upstream_http_{response.status_code}")
            try:
                answer = response.json()
            except ValueError as exc:
                raise AlHaiderError("Al Haider booking answer was not JSON", error_code="invalid_response") from exc
            if isinstance(answer, dict) and _accepted(answer) is False:
                raise SupplierRefused("rejected")
            booking_id = booking_id_of(answer)
            if booking_id is None:  # accepted or silent, but no id: the outcome is unclear
                raise AlHaiderError("Al Haider booking answer had no booking id", error_code="invalid_response")
            detail = None
            try:
                shown = await client.get(f"{self.base_url}/api/show/booking/{booking_id}", headers=self._headers())
                shown.raise_for_status()
                payload = shown.json()
                booking = payload.get("data", payload) if isinstance(payload, dict) else None
                detail = detail_summary(booking) if isinstance(booking, dict) else None
            except Exception as error:  # noqa: BLE001 - the seats are held either way; the detail is informational
                logger.warning("Al Haider booking not read after hold", extra={"error_code": type(error).__name__})
        return HeldSeats(pnr=ticket.pnr, supplier_ref=str(booking_id), cost_per_adult=cost,
                         supplier_answer={"booking_id": booking_id, "create_booking": answer, "detail": detail})

    async def price(self, held) -> GroupMoney:
        raise AlHaiderError("Al Haider has no price re-check for a hold", error_code="not_supported")

    async def release(self, held) -> None:
        raise AlHaiderError("Al Haider release is not used", error_code="not_supported")

    async def issue(self, held, cost_per_adult: GroupMoney) -> list[str]:
        raise AlHaiderError("Al Haider ticket issuing is not built", error_code="not_supported")
