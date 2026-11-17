from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.api.routes.admin_users import router
from app.core.authentication import AuthenticatedUser, require_authenticated_user
from app.db.users import ensure_user


@pytest.fixture
def administration(database):
    connection, _, _ = database
    admin = AuthenticatedUser(
        issuer="https://test.invalid", subject="synthetic-admin", client_id="testclient"
    )
    customer = AuthenticatedUser(
        issuer="https://test.invalid",
        subject="synthetic-customer",
        client_id="testclient",
        groups=("super_admin",),
    )
    first, second = ensure_user(connection, admin), ensure_user(connection, customer)
    connection.execute(
        text("UPDATE users SET role_id=3 WHERE id=:id"), {"id": first.id}
    )
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_authenticated_user] = lambda: admin
    with TestClient(app) as client:
        yield client, app, first.id, second.id, customer, connection


def test_admin_user_listing_roles_counts_and_changes(administration):
    client, _, admin, customer, _, _ = administration
    assert len(client.get("/v1/admin/users").json()) == 2
    filtered = client.get("/v1/admin/users?role_id=1")
    assert filtered.status_code == 200
    assert len(filtered.json()) == 1
    assert filtered.json()[0]["role_id"] == 1
    assert client.get("/v1/admin/roles").json() == [
        {"id": 1, "name": "customer"},
        {"id": 2, "name": "agency_owner"},
        {"id": 3, "name": "super_admin"},
        {"id": 4, "name": "staff"},
    ]
    counts = client.get("/v1/admin/dashboard/overview").json()
    assert counts["user_coverage"] == "local_profiles"
    assert counts["total_users"] == 2 and counts["super_admins"] == 1
    base = f"/v1/admin/users/{customer}"
    assert client.get(base).json()["role"] == "customer"
    response = client.patch(
        base + "/role",
        json={
            "role_id": 3,
            "expected_revision": 1,
            "reason": "Assigned operations administrator",
        },
    )
    assert response.status_code == 200
    assert response.json()["role"] == "super_admin"
    assert (
        client.patch(
            base + "/status",
            json={"active": False, "expected_revision": 1, "reason": "Stale"},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            base + "/status",
            json={"active": False, "expected_revision": 2, "reason": "Access paused"},
        ).status_code
        == 200
    )
    assert len(client.get("/v1/admin/users?active=false").json()) == 1
    assert (
        client.patch(
            base + "/status",
            json={"active": True, "expected_revision": 3, "reason": "Access restored"},
        ).status_code
        == 200
    )
    events = client.get(base + "/history").json()
    assert [event["revision"] for event in events] == [4, 3, 2]
    assert all(str(UUID(event["id"])) == event["id"] for event in events)
    assert all(event["actor_id"] == admin for event in events)


def test_protection_against_self_changes_and_unapproved_owner(administration):
    client, _, admin, customer, _, _ = administration
    assert (
        client.patch(
            f"/v1/admin/users/{admin}/status",
            json={"active": False, "expected_revision": 1, "reason": "Self disable"},
        ).status_code
        == 403
    )
    base = f"/v1/admin/users/{customer}"
    assert (
        client.patch(
            base + "/role",
            json={"role_id": 2, "expected_revision": 1, "reason": "Unapproved"},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            base + "/role",
            json={"role_id": True, "expected_revision": 1, "reason": "Invalid"},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            base + "/status",
            json={"active": "false", "expected_revision": 1, "reason": "Invalid"},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            base + "/status",
            json={"active": False, "expected_revision": 1, "reason": " "},
        ).status_code
        == 422
    )
    assert client.get("/v1/admin/users?limit=101").status_code == 422
    assert client.get("/v1/admin/users?role_id=4").status_code == 422
    assert client.get("/v1/admin/users/invalid").status_code == 422


def test_customer_even_with_admin_group_is_denied(administration):
    client, app, _, customer_id, customer, _ = administration
    app.dependency_overrides[require_authenticated_user] = lambda: customer
    for path in [
        "/users",
        f"/users/{customer_id}",
        f"/users/{customer_id}/history",
        "/roles",
        "/dashboard/overview",
    ]:
        assert client.get("/v1/admin" + path).status_code == 403
    assert (
        client.patch(
            f"/v1/admin/users/{customer_id}/role",
            json={"role_id": 3, "expected_revision": 1, "reason": "Self elevation"},
        ).status_code
        == 403
    )


def test_audit_failure_rolls_back_user_update(administration, monkeypatch):
    client, _, _, customer, _, connection = administration
    execute = connection.execute

    def fail(statement, *args, **kwargs):
        if "INSERT INTO user_admin_events" in str(statement):
            raise OperationalError("private SQL", {}, Exception("private connection"))
        return execute(statement, *args, **kwargs)

    monkeypatch.setattr(connection, "execute", fail)
    response = client.patch(
        f"/v1/admin/users/{customer}/status",
        json={"active": False, "expected_revision": 1, "reason": "Pause"},
    )
    assert response.status_code == 503
    assert "private" not in response.text
    monkeypatch.setattr(connection, "execute", execute)
    result = client.get(f"/v1/admin/users/{customer}").json()
    assert result["active"] is True and result["revision"] == 1
