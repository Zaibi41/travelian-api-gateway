"""Selection is based on synthetic normalized inventory; no supplier calls."""

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes.group_ticketing import router
from app.core.field_visibility import ALL, BackOfficeUser, require_search_viewer
from app.services.group_ticketing import GroupInventoryUnavailable, group_ticket_id
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups


def inventory():
    return normalize_groups(
        [
            {
                "sector": "LHE - RUH",
                "airline": {"name": "Synthetic"},
                "tickets": [
                    {
                        "pnr": "MOCK-001",
                        "date": "2026-12-01",
                        "price": "100",
                        "updated_at": "2026-05-08T05:39:41.000000Z",
                        "sold": "2",
                    }
                ],
            }
        ]
    )


def request(path, groups=None, error=None):
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: BackOfficeUser("u-admin", "super_admin", ALL)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.invalid"
        ) as client:
            return await client.get(path)

    with patch(
        "app.api.routes.group_ticketing.fetch_all_group_inventory",
        new=AsyncMock(return_value=groups, side_effect=error),
    ):
        return asyncio.run(run())


def test_inventory_and_single_selection_preserve_booking_context():
    groups = inventory()
    listing = request("/v1/group-ticketing/inventory", groups).json()
    ticket = listing["data"][0]["tickets"][0]
    response = request(f"/v1/group-ticketing/tickets/{ticket['id']}", groups)
    assert response.status_code == 200
    assert response.json() == ticket
    assert ticket["updated_at"] == "2026-05-08T05:39:41.000000Z"
    assert ticket["sold"] == "2"
    assert ticket["pnr"] == "MOCK-001"
    assert "vendor" not in ticket


def test_selection_id_survives_price_capacity_timestamp_and_group_order_changes():
    ticket = inventory()[0].tickets[0]
    changed = ticket.model_copy(
        update={"price": "200", "remaining": 1, "updated_at": "2026-05-09T00:00:00Z"}
    )
    assert group_ticket_id(ticket) == group_ticket_id(changed)
    assert group_ticket_id(ticket) != group_ticket_id(
        ticket.model_copy(update={"vendor": "skypass"})
    )


@pytest.mark.parametrize(
    "mode,expected", [("missing", 404), ("ambiguous", 409), ("outage", 503)]
)
def test_selection_missing_ambiguous_or_unavailable(mode, expected):
    groups = inventory()
    identifier = group_ticket_id(groups[0].tickets[0])
    if mode == "missing":
        groups = []
    elif mode == "ambiguous":
        groups = groups * 2
    response = request(
        f"/v1/group-ticketing/tickets/{identifier}",
        groups,
        GroupInventoryUnavailable() if mode == "outage" else None,
    )
    assert response.status_code == expected


def test_invalid_ticket_id_is_rejected():
    assert (
        request("/v1/group-ticketing/tickets/invalid", inventory()).status_code == 422
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_canonical_ticket_uuid_and_legacy_url_remain_compatible(legacy):
    groups = inventory()
    identifier = group_ticket_id(groups[0].tickets[0])
    parsed = UUID(identifier)
    assert str(parsed) == identifier
    assert parsed.version == 5
    url_id = parsed.hex if legacy else identifier
    response = request(f"/v1/group-ticketing/tickets/{url_id}", groups)
    assert response.status_code == 200
    assert response.json()["id"] == identifier
