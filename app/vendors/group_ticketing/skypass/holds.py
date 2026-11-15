"""Skypass seat holds (production API: holds are real).

Documented requests (Skypass Postman collection, docs/api/group-holds.md):
- POST /hold-available-seats  form: ticket_id, quantity, open_seats_slot
    -> {"status": "success", "bal_seats": "1", "message": "1 Ticket(s) on hold"}
- POST /release-seats          form: ticket_id, open_seats_slot
    -> {"status": "success", "message": "Ticket on hold free now."}
- GET  /ticket/{id}            -> tickets.open_adult_price (per adult), tickets.airline_pnr
The hold answer carries no hold id, expiry or price: the gateway keeps its own hold record
(expiry = creation + GROUP_HOLD_MINUTES) and takes the price from the listing. Issuing
(submit-booking) is not built: its effect on production is not documented.
Hold and release change supplier state and are never retried here.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.group_ticketing import GroupTicket
from app.schemas.live_flights import HoldContact, HoldPassenger
from app.services.group_ticketing_view import GroupMoney, supplier_cost
from app.vendors.group_ticketing.skypass.inventory import SkypassError, _authenticate

NAME = "skypass"


class SkypassRefused(Exception):
    """The supplier answered clearly that it did nothing."""


def _split_ref(supplier_ref: str) -> tuple[str, str]:
    ticket_id, _, slot = supplier_ref.partition(":")
    if not ticket_id or not slot:
        raise SkypassError("Skypass hold reference is incomplete", error_code="invalid_reference")
    return ticket_id, slot


class SkypassHoldProvider:
    """GroupHoldProvider for Skypass. `transport` lets tests answer the HTTP calls."""

    supports_issue = False
    issue_unavailable_reason = "Issuing is not available for this provider yet"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = (settings.skypass_endpoint or "").rstrip("/")
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(settings.vendor_timeout_seconds, connect=min(settings.vendor_timeout_seconds, 3.0))
        return httpx.AsyncClient(timeout=timeout, transport=self.transport)

    async def _post_form(self, client: httpx.AsyncClient, token: str, path: str, fields: dict[str, str], operation: str) -> dict[str, Any]:
        try:
            response = await client.post(f"{self.base_url}/{path}", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                         files={name: (None, value) for name, value in fields.items()})
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise SkypassError(f"Skypass {operation} timed out", error_code="upstream_timeout") from exc
        except httpx.HTTPStatusError as exc:
            raise SkypassError(f"Skypass {operation} returned HTTP {exc.response.status_code}", error_code=f"upstream_http_{exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SkypassError(f"Skypass {operation} request failed") from exc
        if not isinstance(payload, dict):
            raise SkypassError(f"Skypass {operation} response must be a JSON object", error_code="invalid_response")
        return payload

    async def hold(self, ticket: GroupTicket, passengers: list[HoldPassenger], contact: HoldContact):
        from app.services.group_holds import HeldSeats  # late import: group_holds imports this module

        if not ticket.source_ticket_id or not ticket.source_slot:
            raise SkypassError("Skypass ticket has no id or open_seats_slot", error_code="invalid_ticket")
        cost = supplier_cost(ticket)
        if cost is None:
            raise SkypassError("Skypass ticket has no adult price", error_code="no_price")
        async with self._client() as client:
            token = await _authenticate(client, self.base_url)
            payload = await self._post_form(client, token, "hold-available-seats", {
                "ticket_id": ticket.source_ticket_id, "quantity": str(len(passengers)), "open_seats_slot": ticket.source_slot,
            }, "hold")
        if str(payload.get("status", "")).casefold() != "success":
            raise SkypassRefused(str(payload.get("message") or "not held"))
        return HeldSeats(pnr=(ticket.pnr or "").strip().upper() or None, supplier_ref=f"{ticket.source_ticket_id}:{ticket.source_slot}", cost_per_adult=cost)

    async def price(self, held) -> GroupMoney:
        ticket_id, _ = _split_ref(held.supplier_ref)
        async with self._client() as client:
            token = await _authenticate(client, self.base_url)
            try:
                response = await client.get(f"{self.base_url}/ticket/{ticket_id}", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                raise SkypassError("Skypass ticket detail request failed") from exc
        detail = payload.get("tickets") if isinstance(payload, dict) else None
        raw = detail.get("open_adult_price") if isinstance(detail, dict) else None
        try:
            amount = Decimal(str(raw).replace(",", "").strip())
        except (InvalidOperation, ValueError):
            raise SkypassError("Skypass ticket detail has no usable adult price", error_code="invalid_response") from None
        if amount <= 0:
            raise SkypassError("Skypass ticket detail has no usable adult price", error_code="invalid_response")
        return GroupMoney(amount_minor=int(amount * 100) * max(held.adults, 1), currency=settings.group_ticketing_default_currency)

    async def release(self, held) -> None:
        ticket_id, slot = _split_ref(held.supplier_ref)
        async with self._client() as client:
            token = await _authenticate(client, self.base_url)
            payload = await self._post_form(client, token, "release-seats", {"ticket_id": ticket_id, "open_seats_slot": slot}, "release")
        if str(payload.get("status", "")).casefold() != "success":
            raise SkypassRefused(str(payload.get("message") or "not released"))

    async def issue(self, held, cost_per_adult: GroupMoney) -> list[str]:
        raise SkypassError("Skypass ticket issuing is not built", error_code="not_supported")
