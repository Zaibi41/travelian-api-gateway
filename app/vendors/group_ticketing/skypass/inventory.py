from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.schemas.group_ticketing import GroupInventory, GroupTicket


class SkypassError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "upstream_error") -> None:
        super().__init__(message)
        self.error_code = error_code
        self.public_message = "Supplier request failed."


class SkypassSector(BaseModel):
    model_config = ConfigDict(extra="allow")

    departure: str | None = None
    destination: str | None = None
    departure_date: str | None = None
    departure_time: str | None = None
    arrival_date: str | None = None
    arrival_time: str | None = None
    sector_type: str | None = None


class SkypassTicket(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    name: str | None = None
    image: str | None = None
    service_type: str | None = None
    meal: str | int | None = None
    airline_pnr: str | None = None
    adult_price: str | int | float | None = None
    min_departure_date: str | None = None
    sectors: list[SkypassSector] = Field(default_factory=list)


def is_configured() -> bool:
    return bool(
        settings.skypass_endpoint
        and settings.skypass_client_id
        and settings.skypass_client_secret
        and settings.skypass_client_secret.get_secret_value()
    )


async def fetch_inventory() -> list[GroupInventory]:
    if not is_configured():
        raise SkypassError("Skypass credentials are not configured", error_code="not_configured")

    base_url = (settings.skypass_endpoint or "").rstrip("/")
    timeout = httpx.Timeout(settings.vendor_timeout_seconds, connect=min(settings.vendor_timeout_seconds, 3.0))
    async with httpx.AsyncClient(timeout=timeout) as client:
        token = await _authenticate(client, base_url)
        payload = await _get_tickets(client, base_url, token)

    try:
        tickets = [SkypassTicket.model_validate(row) for row in payload["tickets"]]
    except (KeyError, TypeError, ValidationError) as exc:
        raise SkypassError("Skypass tickets response did not match the documented schema", error_code="invalid_response") from exc

    # The unfiltered inventory endpoint returns every Skypass ticket type.
    return [_normalize_ticket(ticket) for ticket in tickets]


async def _authenticate(client: httpx.AsyncClient, base_url: str) -> str:
    secret = settings.skypass_client_secret
    if secret is None:
        raise SkypassError("Skypass credentials are not configured", error_code="not_configured")
    try:
        response = await client.post(
            f"{base_url}/auth",
            files={
                "api_key": (None, settings.skypass_client_id or ""),
                "api_secret": (None, secret.get_secret_value()),
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise SkypassError("Skypass authentication timed out", error_code="upstream_timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise SkypassError(f"Skypass authentication returned HTTP {exc.response.status_code}", error_code=f"upstream_http_{exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SkypassError("Skypass authentication request failed") from exc

    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise SkypassError("Skypass authentication response did not include access_token", error_code="invalid_response")
    return token


async def _get_tickets(client: httpx.AsyncClient, base_url: str, token: str) -> dict[str, Any]:
    try:
        response = await client.get(
            f"{base_url}/tickets",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise SkypassError("Skypass inventory request timed out", error_code="upstream_timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise SkypassError(f"Skypass inventory returned HTTP {exc.response.status_code}", error_code=f"upstream_http_{exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SkypassError("Skypass inventory request failed") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("tickets"), list):
        raise SkypassError("Skypass inventory response did not include a tickets array", error_code="invalid_response")
    return payload


def _normalize_ticket(ticket: SkypassTicket) -> GroupInventory:
    outbound = [
        sector for sector in ticket.sectors
        if (sector.sector_type or "").strip().casefold() == "out"
    ]
    route = ""
    if outbound and outbound[0].departure and outbound[-1].destination:
        route = f"{outbound[0].departure} - {outbound[-1].destination}"

    departure_date = next(
        (sector.departure_date for sector in outbound if sector.departure_date),
        ticket.min_departure_date,
    )
    subparts = [sector.model_dump(exclude_none=True) for sector in ticket.sectors]
    group_ticket = GroupTicket(
        vendor="skypass",
        pnr=ticket.airline_pnr,
        departure_date=departure_date,
        sector=route or None,
        airline=ticket.name,
        airline_logo=ticket.image,
        price=ticket.adult_price,
        meal=ticket.meal,
        ticket_type=ticket.service_type,
        subparts=subparts,
    )
    return GroupInventory(
        id=f"skypass:{ticket.id}",
        vendor="skypass",
        sector=route or "",
        airline=ticket.name,
        airline_logo=ticket.image,
        tickets=[group_ticket],
    )
