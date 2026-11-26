"""Booking access stays disabled until real identity/ownership is integrated."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes.group_ticketing import router
from app.schemas.group_ticketing import GroupTicket
from app.core.field_visibility import ALL, BackOfficeUser, require_search_viewer
from app.core.config import settings


@pytest.mark.parametrize("endpoint", ["bookings", "booking-details"])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer invalid-test-token"},
        {"X-API-Key": "invalid-test-key"},
        {"X-Role": "admin", "X-Agency-ID": "1", "X-Agent-ID": "2"},
        {"Authorization": "Bearer invalid-test-token", "X-Agency-ID": "2"},
    ],
)
def test_booking_routes_fail_closed_without_supplier_calls(endpoint, headers):
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: BackOfficeUser("u-admin", "super_admin", ALL)

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.invalid"
        ) as client:
            return await client.post(
                f"/v1/group-ticketing/{endpoint}",
                headers=headers,
                json={"agent": 999, "booking_id": 123, "passport_no": ["PRIVATE-TEST"]},
            )

    with (
        patch.object(settings, "cognito_issuer", None),
        patch.object(settings, "cognito_app_client_id", None),
        patch(
            "app.api.routes.group_ticketing._authenticated_dhillu_sahi_client",
            new_callable=AsyncMock,
        ) as supplier,
    ):
        response = asyncio.run(request())
    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication is not configured."}
    supplier.assert_not_called()
    for sensitive in (
        "PRIVATE-TEST",
        "invalid-test-token",
        "invalid-test-key",
        "passport_no",
    ):
        assert sensitive not in response.text


def test_read_only_routes_remain_available_and_get_details_is_absent():
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: BackOfficeUser("u-admin", "super_admin", ALL)
    supplier_client = AsyncMock()
    supplier_client.check_availability.return_value = False

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.invalid"
        ) as client:
            inventory = await client.get("/v1/group-ticketing/inventory")
            search = await client.post(
                "/v1/group-ticketing/search",
                json={
                    "origin": "LHE",
                    "destination": "RUH",
                    "departure_date": "2026-12-01",
                    "adults": 1,
                },
            )
            availability = await client.post(
                "/v1/group-ticketing/check-availability",
                json={
                    "ticket_id": "11111111-1111-4111-8111-111111111111",
                    "adult": 1,
                    "child": 0,
                },
            )
            missing = await client.get("/v1/group-ticketing/bookings/123")
            return inventory, search, availability, missing

    with (
        patch(
            "app.api.routes.group_ticketing.fetch_all_group_inventory",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.api.routes.group_ticketing._credentials_configured", return_value=True
        ),
        patch(  # availability by ticket ID (#107): the fare's PNR and price come from the gateway's own lookup
            "app.api.routes.group_ticketing.find_ticket",
            new=AsyncMock(return_value=GroupTicket(vendor="dhillu_sahi", pnr="MOCK-001", price=100)),
        ),
        patch(
            "app.api.routes.group_ticketing._authenticated_dhillu_sahi_client",
            new=AsyncMock(return_value=(supplier_client, "mock-token")),
        ),
    ):
        inventory, search, availability, missing = asyncio.run(request())
    assert (
        inventory.status_code == search.status_code == availability.status_code == 200
    )
    assert availability.json()["available"] is False
    assert missing.status_code == 404
    supplier_client.add_booking.assert_not_called()
    supplier_client.fetch_booking_details.assert_not_called()
