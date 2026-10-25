from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes import integration_group_ticketing as routes
from app.core.authentication import AuthenticatedMachine
from app.core.config import settings
from app.core.integration_security import require_machine


@pytest.mark.parametrize("permission", [True, False])
def test_machine_search_is_scoped_before_supplier_transport(monkeypatch, permission):
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    machine = AuthenticatedMachine(
        issuer="https://test.invalid",
        client_id="testclient",
        scopes=("test-api/search",),
    )
    app.dependency_overrides[require_machine] = lambda: machine
    monkeypatch.setattr(settings, "integration_search_scope", "test-api/search")
    authorization = AsyncMock(return_value="test-agency")
    if not permission:
        authorization.side_effect = HTTPException(403, "Integration access denied.")
    monkeypatch.setattr(routes, "authorize_machine", authorization)
    inventory = AsyncMock(return_value={"data": []})
    search = AsyncMock(return_value={"data": []})
    monkeypatch.setattr(routes, "fetch_group_inventory", inventory)
    monkeypatch.setattr(routes, "search_group_inventory", search)
    with TestClient(app) as client:
        response = client.get("/v1/integrations/group-ticketing/inventory")
        result = client.post(
            "/v1/integrations/group-ticketing/search",
            json={
                "origin": "LHE",
                "destination": "RUH",
                "departure_date": "2026-12-01",
                "adults": 1,
            },
        )
    assert response.status_code == result.status_code == (200 if permission else 403)
    assert inventory.call_count == search.call_count == (1 if permission else 0)
    authorization.assert_called_with(machine, "test-api/search")
