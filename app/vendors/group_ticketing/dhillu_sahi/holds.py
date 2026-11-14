"""Dhillu & Sahi seat holds through add-booking (production API: bookings are real).

Confirmed by Dhillu & Sahi (14 Nov 2026): add-booking only holds the seats (it does not confirm them);
an unpaid booking expires by itself after 1 hour; there is no cancel or release call.

- POST /api/add-booking (Bearer token, JSON): pnr and updated_at of the ticket from a fresh listing call,
  agent, remarks "", and equal-length arrays titles, firstname, lastname, dob (YYYY-MM-DD), passport_no,
  passport_expiry_date (YYYY-MM-DD), type ("adult"). Answer: {"success": true, "data": {"id": <booking id>, ...}}.
- POST /api/agent-booking-detail (Bearer token, JSON {"booking_id"}): {"success": true, "data": {"data": {...booking},
  "groupdata": {...group}}, "message": ...}. Read right after the hold; read-only.

The hold is Travelian's own 30 minutes (GROUP_HOLD_MINUTES). Nothing is released at our expiry: the supplier ends
the hold after 1 hour. add-booking is never retried: a timeout may have held seats. The token and agent id come from
.env (DHILLU_SAHI_TOKEN, DHILLU_SAHI_AGENT_ID) or from the login; neither is ever logged or returned.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.schemas.group_ticketing import GroupTicket
from app.schemas.live_flights import HoldContact, HoldPassenger
from app.services.group_ticketing_view import GroupMoney, supplier_cost
from app.vendors.group_ticketing.dhillu_sahi.auth import DhilluSahiAuthClient, DhilluSahiAuthError

logger = logging.getLogger(__name__)
NAME = "dhillu_sahi"
#: For viewers with supplier access on a held or expired Dhillu & Sahi hold.
SUPPLIER_HOLD_NOTICE = "Supplier hold ends by itself after 1 hour"
#: Supplier answers that clearly mean nothing was booked (stale updated_at, sold out, invalid input).
REFUSED_HTTP = {400, 404, 409, 410, 422}


class DhilluSahiRefused(Exception):
    """add-booking answered clearly that nothing was booked."""


def is_configured() -> bool:
    password = settings.dhillu_sahi_password
    has_login = bool(settings.dhillu_sahi_email and password and password.get_secret_value())
    token = settings.dhillu_sahi_token
    has_token = bool(token and token.get_secret_value() and settings.dhillu_sahi_agent_id)
    return has_login or has_token


def booking_payload(ticket: GroupTicket, passengers: list[HoldPassenger], agent: int) -> dict[str, Any]:
    """The add-booking body: one entry per traveler in every array, in traveler order. Adults only."""
    if not ticket.pnr or not ticket.updated_at:
        raise ValueError("the listing gave no pnr or updated_at for this ticket")
    if any(passenger.type != "ADT" for passenger in passengers):
        raise ValueError("Dhillu & Sahi holds are for adults only")
    if any(not passenger.document_number or passenger.date_of_birth is None or passenger.document_expiry is None for passenger in passengers):
        raise ValueError("every traveler needs a passport number, date of birth and passport expiry")
    return {
        "pnr": ticket.pnr,
        "agent": agent,
        "updated_at": ticket.updated_at,
        "remarks": "",
        "titles": [passenger.title for passenger in passengers],
        "firstname": [passenger.first_name for passenger in passengers],
        "lastname": [passenger.last_name for passenger in passengers],
        "dob": [passenger.date_of_birth.isoformat() for passenger in passengers],
        "passport_no": [passenger.document_number for passenger in passengers],
        "passport_expiry_date": [passenger.document_expiry.isoformat() for passenger in passengers],
        "type": ["adult" for _ in passengers],
    }


def missing_traveler_details(passengers: list[HoldPassenger]) -> list[tuple[int, str]]:
    """(position, field) for travelers add-booking can't take: it needs a passport for everyone."""
    problems = []
    for index, passenger in enumerate(passengers):
        if passenger.id_type != "PSPT" or not passenger.document_number:
            problems.append((index, "document_number"))
        elif passenger.document_expiry is None:
            problems.append((index, "document_expiry"))
    return problems


#: Booking-detail fields kept on our record (the traveler arrays it echoes stay out: they are already on the booking).
DETAIL_TRAVELER_KEYS = {"booking_sub", "passengers", "passenger", "pax"}


def detail_summary(details: dict[str, Any]) -> dict[str, Any]:
    booking = {k: v for k, v in (details.get("booking") or {}).items() if k not in DETAIL_TRAVELER_KEYS}
    group = details.get("group") or {}
    return {"booking": booking, "group": {k: group.get(k) for k in ("id", "pnr", "price", "updated_at") if k in group},
            "message": details.get("message")}


class DhilluSahiHoldProvider:
    """GroupHoldProvider for Dhillu & Sahi. `transport` lets tests answer the HTTP calls."""

    supports_issue = False
    issue_unavailable_reason = "Issuing is not available for this provider yet"
    can_release = False
    supplier_hold_notice = SUPPLIER_HOLD_NOTICE

    def __init__(self, transport=None) -> None:
        self.client = DhilluSahiAuthClient(settings.dhillu_sahi_base_url, timeout_seconds=settings.dhillu_sahi_timeout_seconds,
                                           transport=transport)

    def check_passengers(self, passengers: list[HoldPassenger]) -> list[tuple[int, str]]:
        return missing_traveler_details(passengers)

    async def _session(self) -> tuple[str, int]:
        """(token, agent id): from .env when both are set, otherwise from the login."""
        token = settings.dhillu_sahi_token.get_secret_value() if settings.dhillu_sahi_token else ""
        if token and settings.dhillu_sahi_agent_id:
            return token, settings.dhillu_sahi_agent_id
        password = settings.dhillu_sahi_password
        login = await self.client.login(settings.dhillu_sahi_email or "", password.get_secret_value() if password else "")
        agent = self.client.agent_id
        if agent is None:
            raise DhilluSahiAuthError("Supplier login did not give an agent", error_code="agent_identity_unavailable")
        if settings.dhillu_sahi_agent_id is not None and settings.dhillu_sahi_agent_id != agent:
            raise DhilluSahiAuthError("DHILLU_SAHI_AGENT_ID does not match the supplier login", error_code="agent_identity_mismatch")
        return login["token"], agent

    async def hold(self, ticket: GroupTicket, passengers: list[HoldPassenger], contact: HoldContact):
        from app.services.group_holds import HeldSeats, SupplierRefused  # late import: group_holds imports this module

        cost = supplier_cost(ticket)
        if cost is None:
            raise DhilluSahiAuthError("Dhillu & Sahi ticket has no price", error_code="no_price")
        token, agent = await self._session()
        body = booking_payload(ticket, passengers, agent)
        try:
            answer = await self.client.create_booking(token, body)  # never retried
        except DhilluSahiAuthError as error:
            status = error.error_code.removeprefix("upstream_http_")
            if error.error_code == "rejected" or (status.isdigit() and int(status) in REFUSED_HTTP):
                raise SupplierRefused(error.error_code) from None
            raise
        booking_id = answer["data"]["id"]
        details = None
        try:
            details = detail_summary(await self.client.fetch_booking_details(token, booking_id))
        except Exception as error:  # noqa: BLE001 - the seats are held either way; the detail is informational
            logger.warning("Dhillu & Sahi booking detail not read after hold", extra={"error_code": getattr(error, "error_code", type(error).__name__)})
        return HeldSeats(pnr=ticket.pnr, supplier_ref=str(booking_id), cost_per_adult=cost,
                         supplier_answer={"booking_id": booking_id, "add_booking": answer, "detail": details})

    async def price(self, held) -> GroupMoney:
        raise DhilluSahiAuthError("Dhillu & Sahi has no price re-check for a hold", error_code="not_supported")

    async def release(self, held) -> None:
        raise DhilluSahiAuthError("Dhillu & Sahi has no release or cancel call", error_code="not_supported")

    async def issue(self, held, cost_per_adult: GroupMoney) -> list[str]:
        raise DhilluSahiAuthError("Dhillu & Sahi ticket issuing is not built", error_code="not_supported")
