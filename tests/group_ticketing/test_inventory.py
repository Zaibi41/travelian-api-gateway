from uuid import UUID

"""Inventory routes exercised with synthetic credentials and mocked HTTP only."""

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.api.routes.group_ticketing import router
from app.core.field_visibility import ALL, BackOfficeUser, require_search_viewer
from app.core.config import settings

REAL_ASYNC_CLIENT = httpx.AsyncClient


def exercise(
    dhillu="success", skypass="success", configured=(True, True), concurrent=False
):
    calls = []
    arrivals = set()
    expected = sum(configured)
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_search_viewer] = lambda: BackOfficeUser("u-admin", "super_admin", ALL)

    async def run():
        ready = asyncio.Event()

        async def supplier(request):
            path = request.url.path
            calls.append(path)
            if path == "/api/login":
                return httpx.Response(
                    200, json={"success": {"token": "synthetic-token"}}
                )
            if path == "/auth":
                return httpx.Response(200, json={"access_token": "synthetic-token"})
            assert path in ("/api/pnr-respect-to-mysector-user", "/tickets")
            if concurrent:
                arrivals.add(path)
                if len(arrivals) == expected:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=1)
            mode = dhillu if path.startswith("/api/") else skypass
            if mode == "failure":
                return httpx.Response(
                    503, json={"message": "synthetic supplier failure"}
                )
            if mode == "deadline":
                await asyncio.sleep(0.1)
            if mode == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            if mode == "malformed":
                return httpx.Response(200, json={"success": True, "data": [{}]})
            if path.startswith("/api/"):
                assert json.loads(request.content) == {
                    "my_sector_id": "synthetic-sector"
                }
                rows = (
                    []
                    if mode == "empty"
                    else [
                        {
                            "id": 77,
                            "sector": "LHE - RUH",
                            "airline": {"name": "Synthetic Airline"},
                            "tickets": [
                                {
                                    "pnr": "MOCK-001",
                                    "date": "2026-12-01",
                                    "price": "100",
                                    "remaining": 3,
                                    "meal": 5,
                                    "baggage": "20 KG",
                                    "ticket_type": "One Way",
                                    "subpart": [{"synthetic_leg": "outbound"}],
                                }
                            ],
                        }
                    ]
                )
                return httpx.Response(200, json={"success": True, "data": rows})
            rows = (
                []
                if mode == "empty"
                else [
                    {
                        "id": 77,
                        "name": "Synthetic Airline",
                        "airline_pnr": "MOCK/002",
                        "adult_price": 200,
                        "service_type": "Two Way",
                        "sectors": [
                            {
                                "departure": "KHI",
                                "destination": "DXB",
                                "departure_date": "2026-12-02",
                                "sector_type": "out",
                            },
                            {
                                "departure": "DXB",
                                "destination": "KHI",
                                "departure_date": "2026-12-09",
                                "sector_type": "in",
                            },
                        ],
                    }
                ]
            )
            return httpx.Response(200, json={"tickets": rows})

        transport = httpx.MockTransport(supplier)

        def client_factory(*args, **kwargs):
            if kwargs.get("transport") is None:
                kwargs["transport"] = transport
            return REAL_ASYNC_CLIENT(*args, **kwargs)

        with (
            patch.object(
                settings,
                "dhillu_sahi_email",
                "synthetic@example.invalid" if configured[0] else None,
            ),
            patch.object(
                settings,
                "dhillu_sahi_password",
                SecretStr("synthetic-password") if configured[0] else None,
            ),
            patch.object(
                settings,
                "dhillu_sahi_group_sector_id",
                "synthetic-sector" if configured[0] else None,
            ),
            # One Dhillu & Sahi sector group (the legacy single value); Al Haider not configured here.
            patch.object(settings, "dhillu_sahi_sector_group_ids", ""),
            patch.object(settings, "al_haider_base_url", None),
            patch.object(settings, "al_haider_access_token", None),
            patch.object(settings, "dhillu_sahi_base_url", "https://dhillu.invalid"),
            patch.object(
                settings,
                "skypass_endpoint",
                "https://skypass.invalid" if configured[1] else None,
            ),
            patch.object(
                settings,
                "skypass_client_id",
                SecretStr("synthetic-key") if configured[1] else None,
            ),
            patch.object(
                settings,
                "skypass_client_secret",
                SecretStr("synthetic-secret") if configured[1] else None,
            ),
            patch.object(settings, "vendor_max_concurrency", 2),
            patch.object(
                settings,
                "vendor_timeout_seconds",
                0.02 if "deadline" in (dhillu, skypass) else 2,
            ),
            patch("httpx.AsyncClient", side_effect=client_factory),
        ):
            async with REAL_ASYNC_CLIENT(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway.invalid",
            ) as api:
                return await api.get("/v1/group-ticketing/inventory")

    return asyncio.run(run()), calls


def test_combines_both_adapters_without_search_filtering():
    response, calls = exercise(concurrent=True)
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data"}
    groups = payload["data"]
    assert len(groups) == 2
    assert {group["sector"] for group in groups} == {"LHE - RUH", "KHI - DXB"}
    assert len({group["id"] for group in groups}) == 2
    assert all(str(UUID(group["id"])) == group["id"] for group in groups)
    assert all(UUID(group["id"]).version == 5 for group in groups)
    # Supplier logo URLs never leave the gateway; groups carry a gateway logo path.
    assert all(set(group) <= {"id", "sector", "airline", "logo_url", "tickets"} for group in groups)
    tickets = [ticket for group in groups for ticket in group["tickets"]]
    assert {ticket["pnr"] for ticket in tickets} == {"MOCK-001", "MOCK/002"}
    assert all("vendor" not in ticket and "airline_logo" not in ticket for ticket in tickets)
    assert {ticket["ticket_type"] for ticket in tickets} == {"One Way", "Two Way"}
    first = next(ticket for ticket in tickets if ticket["pnr"] == "MOCK-001")
    assert first["remaining"] == 3
    assert "meal" not in first  # the supplier sends 0/1; an unknown value (5) is not shown as Yes or No
    assert "price" not in first  # super admin: supplier_cost instead (D23)
    assert first["supplier_cost"] == {"amount_minor": 10_000, "currency": "PKR"}
    assert first["subparts"] == [{"synthetic_leg": "outbound"}]
    second = next(ticket for ticket in tickets if ticket["pnr"] == "MOCK/002")
    assert "remaining" not in second  # unknown: omitted
    assert "currency" not in second
    assert len(second["subparts"]) == 2
    assert (
        calls.count("/tickets") == calls.count("/api/pnr-respect-to-mysector-user") == 1
    )


@pytest.mark.parametrize("mode", ["failure", "timeout", "deadline", "malformed"])
@pytest.mark.parametrize("failed_vendor", ["dhillu", "skypass"])
def test_partial_failures_preserve_successful_inventory(mode, failed_vendor, caplog):
    response, _ = exercise(**{failed_vendor: mode})
    assert response.status_code == 200
    assert set(response.json()) == {"data"}
    assert len(response.json()["data"]) == 1
    expected = "KHI - DXB" if failed_vendor == "dhillu" else "LHE - RUH"
    assert response.json()["data"][0]["sector"] == expected
    assert any(
        record.message == "Group inventory supplier unavailable"
        for record in caplog.records
    )


@pytest.mark.parametrize("configured", [(False, False), (True, True)])
def test_unavailable_inventory_returns_generic_503(configured):
    response, calls = exercise("failure", "failure", configured=configured)
    assert response.status_code == 503
    assert response.json() == {"detail": "Group inventory is temporarily unavailable"}
    if not any(configured):
        assert calls == []


@pytest.mark.parametrize("other", ["empty", "failure", "timeout"])
def test_successful_empty_inventory_is_not_an_outage(other):
    response, _ = exercise("empty", other)
    assert response.status_code == 200
    assert response.json() == {"data": []}


@pytest.mark.parametrize("configured", [(True, False), (False, True)])
def test_only_configured_vendor_is_called(configured):
    response, calls = exercise(configured=configured)
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1
    assert ("/api/login" in calls) is configured[0]
    assert ("/auth" in calls) is configured[1]
