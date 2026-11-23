"""Synthetic PNRs only; all supplier HTTP is intercepted by MockTransport."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.group_ticketing import router
from app.core.booking_security import require_booking_authorization
from app.core.field_visibility import ALL, BackOfficeUser, require_search_viewer
from app.services.group_ticketing import group_ticket_id
from app.vendors.group_ticketing.dhillu_sahi.auth import DhilluSahiAuthClient
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups

# gateway#107: check-availability takes the fare's ticket ID; the gateway reads the PNR and supplier price from
# inventory. These are the PNRs the synthetic inventory holds (supplier price 100 each).
INVENTORY_PNRS = ["TeSt01", "Te/St-01", "Te+St_01", 'Te"St\\01', "X", "X" * 32, "MOCK-001"]


def inventory():
    return normalize_groups([{"sector": "LHE - RUH", "airline": {"name": "Synthetic Air"}, "tickets": [
        {"pnr": pnr, "date": "2026-12-01", "price": "100", "remaining": 9, "subpart": [{"departure_time": "09:45"}]} for pnr in INVENTORY_PNRS]}])


def ticket_for(pnr):
    return next(group_ticket_id(t) for g in inventory() for t in g.tickets if t.pnr == pnr)


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
    app.dependency_overrides[require_search_viewer] = lambda: BackOfficeUser("u-admin", "super_admin", ALL)
    app.include_router(router, prefix="/v1")
    with (
        patch(
            "app.api.routes.group_ticketing._credentials_configured", return_value=True
        ),
        patch("app.api.routes.group_ticketing.fetch_all_group_inventory", new=AsyncMock(side_effect=lambda: inventory())),
        patch(
            "app.api.routes.group_ticketing._authenticated_dhillu_sahi_client",
            new=AsyncMock(return_value=(client, "synthetic-token")),
        ),
        TestClient(app) as api,
    ):
        yield api, seen


def payload(endpoint, pnr):
    if endpoint == "check-availability":
        return {"ticket_id": ticket_for(pnr), "adult": 1, "child": 0}
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


@pytest.mark.parametrize("endpoint", ["bookings"])  # check-availability takes a ticket ID, not a PNR (gateway#107)
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
            "ticket_id": ticket_for("MOCK-001"),
            "passenger_types": ["adult", "child", "infant"],
        },
    )
    assert response.json()["status"] == "complete"
    assert seen[0][1] == {"pnr": "MOCK-001", "price": 100, "adult": 1, "child": 2}  # PNR and price from inventory


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
            "ticket_id": ticket_for("MOCK-001"),
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


@pytest.mark.parametrize("extra", [{"pnr": "MOCK-001"}, {"price": 1}, {"pnr": "MOCK-001", "price": 100}])
def test_availability_refuses_a_pnr_or_price_from_the_caller(gateway, extra):
    """gateway#107: the supplier price never comes from (or goes to) the caller."""
    api, seen = gateway
    response = api.post("/v1/group-ticketing/check-availability",
                        json={"ticket_id": ticket_for("MOCK-001"), "adult": 1, "child": 0, **extra})
    assert response.status_code == 422 and seen == []


def test_availability_for_a_fare_no_longer_in_inventory_is_404_without_a_supplier_call(gateway):
    api, seen = gateway
    response = api.post("/v1/group-ticketing/check-availability",
                        json={"ticket_id": "11111111-1111-4111-8111-111111111111", "adult": 1, "child": 0})
    assert response.status_code == 404 and seen == []


def test_availability_for_another_suppliers_fare_says_holding_checks_the_seats(gateway):
    from app.schemas.group_ticketing import GroupInventory, GroupTicket

    api, seen = gateway
    other = GroupTicket(vendor="skypass", pnr="SKY1", departure_date="2026-12-01", sector="LHE-RUH", price="100")
    with patch("app.api.routes.group_ticketing.fetch_all_group_inventory",
               new=AsyncMock(return_value=[GroupInventory(id="g", vendor="skypass", sector="LHE-RUH", tickets=[other])])):
        response = api.post("/v1/group-ticketing/check-availability", json={"ticket_id": group_ticket_id(other), "adult": 1, "child": 0})
    assert response.status_code == 422 and "Holding it checks the seats" in response.json()["detail"]
    assert "skypass" not in response.text.lower() and seen == []
