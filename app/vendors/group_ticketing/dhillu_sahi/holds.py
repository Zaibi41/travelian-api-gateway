"""Dhillu & Sahi seat holds through add-booking (production API: bookings are real).

Contract: Dhillu & Sahi APIs Integration Manual (PDF, not in the repo; read 14 Nov 2026) and the Postman collection.
Confirmed by Dhillu & Sahi: add-booking only holds the seats (it does not confirm them); an unpaid booking expires by
itself after 1 hour; there is no cancel or release call.

1. POST /api/check-availbility (spelled that way) {"pnr", "adult", "child", "price"} -> {"success": true, "data":
   {"status": true|false}, "message"}. `price` is documented as "Current price showing": the supplier's listing price
   per seat (not Travelian's selling price with markup). status false: the price or the seats changed; nothing is booked.
2. POST /api/add-booking {pnr, agent, updated_at, remarks, titles[], firstname[], lastname[], dob[], passport_no[],
   passport_expiry_date[], type[]} -> {"success": true, "data": {"id": <booking id>}, "message"}. titles: MR, MRS, MS,
   MSTR, MISS. `agent` is the login's agent_id (not its user_id), or DHILLU_SAHI_AGENT_ID when set.
3. POST /api/agent-booking-detail {"booking_id"} -> {"success": true, "data": {"data": {id, agent, pnr, booking_date,
   confirmed ('1' = confirmed), total_amount, final_total, received_amount, remaining_amount (strings, PKR),
   cancel_status ('0' active, '1' cancelled), cancel_at, booking_sub[], payments[], ...}, "groupdata": {id, pnr, price,
   airline, subpart[]}}, "message"}. total_amount is the booking's total (all travelers), not per adult.
   `confirmed` is not read as paid or ticketed.

The hold is Travelian's own 30 minutes (GROUP_HOLD_MINUTES). Nothing is released at our expiry. add-booking is never
retried: a timeout may have held seats. The token and agent id are never logged or returned.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
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
#: Our traveler titles -> the titles add-booking accepts (MR, MRS, MS, MSTR, MISS).
TITLES = {"MR": "MR", "MRS": "MRS", "MS": "MS", "MISS": "MISS", "MASTER": "MSTR", "MSTR": "MSTR"}
#: The booking amounts and states kept on our record from agent-booking-detail.
AMOUNT_KEYS = ("total_amount", "final_total", "received_amount", "remaining_amount", "cancel_status", "confirmed", "cancel_at", "booking_date")


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
    if any(passenger.title not in TITLES for passenger in passengers):
        raise ValueError("titles must be MR, MRS, MS, MSTR or MISS")
    return {
        "pnr": ticket.pnr,
        "agent": agent,
        "updated_at": ticket.updated_at,
        "remarks": "",
        "titles": [TITLES[passenger.title] for passenger in passengers],
        "firstname": [passenger.first_name for passenger in passengers],
        "lastname": [passenger.last_name for passenger in passengers],
        "dob": [passenger.date_of_birth.isoformat() for passenger in passengers],
        "passport_no": [passenger.document_number for passenger in passengers],
        "passport_expiry_date": [passenger.document_expiry.isoformat() for passenger in passengers],
        "type": ["adult" for _ in passengers],
    }


def missing_traveler_details(passengers: list[HoldPassenger]) -> list[tuple[int, str]]:
    """(position, field) for travelers add-booking can't take: a title it accepts and a passport for everyone."""
    problems = []
    for index, passenger in enumerate(passengers):
        if passenger.title not in TITLES:
            problems.append((index, "title"))
        elif passenger.id_type != "PSPT" or not passenger.document_number:
            problems.append((index, "document_number"))
        elif passenger.document_expiry is None:
            problems.append((index, "document_expiry"))
    return problems


def listing_price(cost: GroupMoney) -> int | float:
    """The listing price per seat in rupees, as check-availbility expects it (78000, not 7800000 paisa)."""
    rupees = Decimal(cost.amount_minor) / 100
    return int(rupees) if rupees == rupees.to_integral_value() else float(rupees)


def amounts(booking: dict[str, Any]) -> dict[str, Any]:
    """total_amount, final_total, received_amount, remaining_amount, cancel_status, confirmed, cancel_at, booking_date."""
    return {key: booking.get(key) for key in AMOUNT_KEYS if key in booking}


def total_money(value: Any) -> GroupMoney | None:
    """'45000' (PKR, the booking's total) -> GroupMoney in paisa; None when not a positive amount."""
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return GroupMoney(amount_minor=int(amount * 100), currency="PKR")


def is_cancelled(booking_amounts: dict[str, Any]) -> bool:
    return str(booking_amounts.get("cancel_status", "")).strip() == "1"


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
        """(token, agent): DHILLU_SAHI_TOKEN and DHILLU_SAHI_AGENT_ID when both are set; otherwise the login's token,
        with DHILLU_SAHI_AGENT_ID if set, else the login's agent_id (never its user_id)."""
        token = settings.dhillu_sahi_token.get_secret_value() if settings.dhillu_sahi_token else ""
        if token and settings.dhillu_sahi_agent_id:
            return token, settings.dhillu_sahi_agent_id
        password = settings.dhillu_sahi_password
        login = await self.client.login(settings.dhillu_sahi_email or "", password.get_secret_value() if password else "")
        if settings.dhillu_sahi_agent_id is not None:
            if self.client.agent_id is not None and self.client.agent_id != settings.dhillu_sahi_agent_id:
                logger.warning("DHILLU_SAHI_AGENT_ID differs from the login's agent_id; using DHILLU_SAHI_AGENT_ID")
            return login["token"], settings.dhillu_sahi_agent_id
        if self.client.agent_id is None:
            raise DhilluSahiAuthError("Supplier login did not give an agent_id", error_code="agent_identity_unavailable")
        return login["token"], self.client.agent_id

    async def _available(self, token: str, pnr: str, adults: int, cost: GroupMoney) -> bool:
        return await self.client.check_availability(token, pnr=pnr, adult=adults, child=0, price=listing_price(cost))

    async def hold(self, ticket: GroupTicket, passengers: list[HoldPassenger], contact: HoldContact):
        from app.services.group_holds import HeldSeats, PriceOrSeatsChanged, SupplierRefused  # late import: group_holds imports this module

        cost = supplier_cost(ticket)
        if cost is None:
            raise DhilluSahiAuthError("Dhillu & Sahi ticket has no price", error_code="no_price")
        token, agent = await self._session()
        body = booking_payload(ticket, passengers, agent)
        if not await self._available(token, ticket.pnr or "", len(passengers), cost):
            raise PriceOrSeatsChanged("availability_changed")  # nothing booked: search again
        try:
            answer = await self.client.create_booking(token, body)  # never retried
        except DhilluSahiAuthError as error:
            status = error.error_code.removeprefix("upstream_http_")
            if error.error_code == "rejected" or (status.isdigit() and int(status) in REFUSED_HTTP):
                raise SupplierRefused(error.error_code) from None
            raise
        booking_id = answer["data"]["id"]
        booking_amounts, held_total = None, None
        try:
            details = await self.client.fetch_booking_details(token, booking_id)
            booking_amounts = amounts(details["booking"])
            held_total = total_money(booking_amounts.get("total_amount"))
        except Exception as error:  # noqa: BLE001 - the seats are held either way; the listing price stands
            logger.warning("Dhillu & Sahi booking detail not read after hold", extra={"error_code": getattr(error, "error_code", type(error).__name__)})
        return HeldSeats(pnr=ticket.pnr, supplier_ref=str(booking_id), cost_per_adult=cost, supplier_total=held_total,
                         supplier_answer={"booking_id": booking_id, "add_booking": answer, "amounts": booking_amounts})

    async def refresh(self, held) -> dict[str, Any]:
        """The booking's amounts and states now (agent-booking-detail). Read-only."""
        token, _ = await self._session()
        details = await self.client.fetch_booking_details(token, int(held.supplier_ref))
        return amounts(details["booking"])

    async def price(self, held) -> GroupMoney:
        """Before issuing: check-availbility with the held price per adult (status false -> AvailabilityChanged), then the
        booking's total_amount from agent-booking-detail, per adult."""
        from app.services.group_holds import PriceOrSeatsChanged  # late import: group_holds imports this module

        if held.cost_per_adult is None or not held.pnr:
            raise DhilluSahiAuthError("No held price to check", error_code="no_price")
        token, _ = await self._session()
        if not await self._available(token, held.pnr, held.adults, held.cost_per_adult):
            raise PriceOrSeatsChanged("availability_changed")
        details = await self.client.fetch_booking_details(token, int(held.supplier_ref))
        total = total_money(details["booking"].get("total_amount"))
        if total is None:
            raise DhilluSahiAuthError("Dhillu & Sahi booking detail had no total_amount", error_code="invalid_response")
        return GroupMoney(amount_minor=total.amount_minor // max(held.adults, 1), currency=total.currency)

    async def release(self, held) -> None:
        raise DhilluSahiAuthError("Dhillu & Sahi has no release or cancel call", error_code="not_supported")

    async def issue(self, held, cost_per_adult: GroupMoney) -> list[str]:
        raise DhilluSahiAuthError("Dhillu & Sahi ticket issuing is not built", error_code="not_supported")
