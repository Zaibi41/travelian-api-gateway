"""Contract checks with synthetic data and intercepted supplier HTTP only."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.api.routes.group_ticketing import (
    GroupBookingRequest,
    _create_dhillu_sahi_booking,
)
from app.vendors.group_ticketing.dhillu_sahi.auth import (
    DhilluSahiAuthClient,
    DhilluSahiAuthError,
)


@pytest.mark.parametrize("agent", [None, True, 0, -1, "123"])
def test_missing_or_invalid_login_agent_never_creates_booking(agent):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(
            200, json={"success": {"token": "synthetic-token", "agent_id": agent}}
        )

    client = DhilluSahiAuthClient(
        "https://supplier.invalid", transport=httpx.MockTransport(handler)
    )
    body = GroupBookingRequest.model_validate(
        {
            "pnr": "MOCK-001",
            "updated_at": "2026-05-08T05:39:41.000000Z",
            "titles": ["MR"],
            "firstname": ["Test"],
            "lastname": ["Person"],
            "dob": ["1990-01-01"],
            "passport_no": ["MOCK123"],
            "passport_expiry_date": ["2030-01-01"],
            "type": ["adult"],
        }
    )

    async def run():
        login = await client.login("test@example.invalid", "synthetic-password")
        with patch(
            "app.api.routes.group_ticketing._authenticated_dhillu_sahi_client",
            new=AsyncMock(return_value=(client, login["token"])),
        ):
            with pytest.raises(DhilluSahiAuthError) as error:
                await _create_dhillu_sahi_booking(body)
            assert error.value.error_code == "agent_identity_unavailable"

    asyncio.run(run())
    assert paths == ["/api/login"]


@pytest.mark.parametrize("identifier", [True, False, 0, -1, "123", None])
def test_invalid_supplier_booking_id_is_not_success(identifier):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"success": True, "data": {"id": identifier}})

    client = DhilluSahiAuthClient(
        "https://supplier.invalid", transport=httpx.MockTransport(handler)
    )

    async def run():
        with pytest.raises(DhilluSahiAuthError):
            await client.add_booking("synthetic-token", {"pnr": "MOCK-001"})

    asyncio.run(run())
    assert seen == ["/api/add-booking"]
