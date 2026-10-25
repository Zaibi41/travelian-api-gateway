"""Synthetic PNRs only; all supplier HTTP is intercepted by MockTransport."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.group_ticketing import router
from app.core.booking_security import require_booking_authorization
from app.vendors.group_ticketing.dhillu_sahi.auth import DhilluSahiAuthClient


@pytest.fixture
def gateway():
    seen = []

    def supplier(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path == "/api/check-availbility":
            return httpx.Response(
                200, json={"success": True, "data": {"status": False}}
            )
        if request.url.path == "/api/add-booking":
            return httpx.Response(200, json={"success": True, "data": {"id": 123}})
        if request.url.path == "/api/agent-booking-detail":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "data": {"id": 123, "pnr": "Te/St-01"},
                        "groupdata": {"pnr": "Te/St-01"},
                    },
                },
            )
        raise AssertionError("Unexpected supplier operation")

    client = DhilluSahiAuthClient(
        "https://supplier.invalid", transport=httpx.MockTransport(supplier)
    )
    client.agent_id = 1  # Synthetic supplier-authenticated agent for mapping tests.
    app = FastAPI()
    # Isolate PNR mapping tests; production booking access remains disabled.
    app.dependency_overrides[require_booking_authorization] = lambda: None
    app.include_router(router, prefix="/v1")
    with (
        patch(
            "app.api.routes.group_ticketing._credentials_configured", return_value=True
        ),
        patch(
            "app.api.routes.group_ticketing._authenticated_dhillu_sahi_client",
            new=AsyncMock(return_value=(client, "synthetic-token")),
        ),
        TestClient(app) as api,
    ):
        yield api, seen


def payload(endpoint, pnr):
    if endpoint == "check-availability":
        return {"pnr": pnr, "adult": 1, "child": 0, "price": 100}
    return {
        "pnr": pnr,
        "agent": 1,
        "updated_at": "2026-10-23T00:00:00Z",
        "titles": ["MR"],
        "firstname": ["Test"],
        "lastname": ["Passenger"],
        "dob": ["1990-01-01"],
        "passport_no": ["TEST123"],
        "passport_expiry_date": ["2030-01-01"],
        "type": ["adult"],
    }


@pytest.mark.parametrize("endpoint", ["check-availability", "bookings"])
@pytest.mark.parametrize(
    "pnr", ["TeSt01", "Te/St-01", "Te+St_01", 'Te"St\\01', "X", "X" * 32]
)
def test_pnr_is_forwarded_unchanged(gateway, endpoint, pnr):
    api, seen = gateway
    response = api.post(f"/v1/group-ticketing/{endpoint}", json=payload(endpoint, pnr))
    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert len(seen) == 1  # Includes booking: exactly one mocked request, no retry.
    expected = (
        "/api/check-availbility"
        if endpoint == "check-availability"
        else "/api/add-booking"
    )
    assert seen[0][0] == expected
    assert seen[0][1]["pnr"] == pnr


@pytest.mark.parametrize("endpoint", ["check-availability", "bookings"])
@pytest.mark.parametrize(
    "pnr",
    [
        "",
        "X" * 33,
        " ",
        " TEST01",
        "TEST01 ",
        "TE ST01",
        "TE\tST01",
        "TEST01\n",
        "TE\rST01",
        "TE\x00ST01",
        "TE\x1bST01",
        "TE\x7fST01",
        "TÉST01",
        "TE\u200bST01",
        "TE\u00a0ST01",
        None,
        123,
        [],
        {},
    ],
)
def test_invalid_pnr_never_reaches_supplier(gateway, endpoint, pnr):
    api, seen = gateway
    response = api.post(f"/v1/group-ticketing/{endpoint}", json=payload(endpoint, pnr))
    assert response.status_code == 422
    assert any(error["loc"] == ["body", "pnr"] for error in response.json()["detail"])
    assert seen == []


def test_booking_details_uses_id_and_preserves_response_pnr(gateway):
    api, seen = gateway
    response = api.post("/v1/group-ticketing/booking-details", json={"booking_id": 123})
    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert seen == [("/api/agent-booking-detail", {"booking_id": 123})]
    assert response.json()["details"]["booking"]["pnr"] == "Te/St-01"
    assert response.json()["details"]["group"]["pnr"] == "Te/St-01"


@pytest.mark.parametrize("booking_id", [0, -1, "invalid"])
def test_invalid_booking_details_id_never_reaches_supplier(gateway, booking_id):
    api, seen = gateway
    response = api.post(
        "/v1/group-ticketing/booking-details", json={"booking_id": booking_id}
    )
    assert response.status_code == 422
    assert seen == []


def test_booking_derives_agent_and_preserves_supplier_timestamp(gateway):
    api, seen = gateway
    body = payload("bookings", "MOCK-001")
    body.pop("agent")
    body["updated_at"] = "2026-05-08T05:39:41.000000Z"
    response = api.post("/v1/group-ticketing/bookings", json=body)
    assert response.json()["status"] == "complete"
    assert seen[0][1]["agent"] == 1
    assert seen[0][1]["updated_at"] == body["updated_at"]


def test_booking_rejects_agent_impersonation_before_creation(gateway):
    api, seen = gateway
    body = payload("bookings", "MOCK-001")
    body["agent"] = 999
    response = api.post("/v1/group-ticketing/bookings", json=body)
    assert response.json()["status"] == "unavailable"
    assert response.json()["vendors_failed"][0]["code"] == "agent_identity_mismatch"
    assert seen == []


def test_passenger_types_combine_children_and_infants(gateway):
    api, seen = gateway
    response = api.post(
        "/v1/group-ticketing/check-availability",
        json={
            "pnr": "MOCK-001",
            "price": 100,
            "passenger_types": ["adult", "child", "infant"],
        },
    )
    assert response.json()["status"] == "complete"
    assert seen[0][1] == {"pnr": "MOCK-001", "price": 100, "adult": 1, "child": 2}


@pytest.mark.parametrize(
    "counts",
    [
        {"passenger_types": ["adult"], "adult": 1, "child": 0},
        {"adult": 0, "child": 0},
        {"adult": 1},
        {},
        {"passenger_types": []},
    ],
)
def test_invalid_or_ambiguous_counts_never_call_supplier(gateway, counts):
    api, seen = gateway
    response = api.post(
        "/v1/group-ticketing/check-availability",
        json={
            "pnr": "MOCK-001",
            "price": 100,
            **counts,
        },
    )
    assert response.status_code == 422
    assert seen == []


@pytest.mark.parametrize("timestamp", ["not-a-date", "2026-05-08T05:39:41", "", None])
def test_invalid_booking_timestamp_never_reaches_supplier(gateway, timestamp):
    api, seen = gateway
    body = payload("bookings", "MOCK-001")
    body["updated_at"] = timestamp
    response = api.post("/v1/group-ticketing/bookings", json=body)
    assert response.status_code == 422
    assert seen == []
