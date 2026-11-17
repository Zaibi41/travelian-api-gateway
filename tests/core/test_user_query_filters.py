"""Query strings must parse before reaching the SQL filter."""

import asyncio
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import admin_users
from app.core.agency_security import require_local_admin


def request(monkeypatch, role):
    store = Mock(return_value=[])
    monkeypatch.setattr(admin_users.store, "list_users", store)
    app = FastAPI()
    app.include_router(admin_users.router, prefix="/v1")

    async def authorized():
        return None

    async def inline_worker(function, *args):
        return function(*args)

    app.dependency_overrides[require_local_admin] = authorized
    # Avoid worker infrastructure in this validation-only test; database and
    # authentication behavior are covered by separate integration tests.
    monkeypatch.setattr(admin_users, "run_in_threadpool", inline_worker)

    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test.invalid"
        ) as client:
            return await client.get(
                "/v1/admin/users",
                params={"role_id": role, "active": "true", "limit": 20, "offset": 0},
            )

    return asyncio.run(call()), store


@pytest.mark.parametrize("role", [1, 2, 3])
def test_numeric_role_query_is_parsed_and_forwarded(role, monkeypatch):
    response, store = request(monkeypatch, role)
    assert response.status_code == 200
    assert response.json() == []
    store.assert_called_once_with(20, 0, role, True)


@pytest.mark.parametrize("role", ["0", "4", "-1", "abc", "2.5", ""])
def test_invalid_role_query_never_reaches_store(role, monkeypatch):
    response, store = request(monkeypatch, role)
    assert response.status_code == 422
    store.assert_not_called()
