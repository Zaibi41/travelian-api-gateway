from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.agencies import router
from app.core.authentication import AuthenticatedUser, require_authenticated_user
from app.core.config import settings
from app.db import agencies, agency_applications


@pytest.fixture
def management(database, monkeypatch):
    connection, _, _ = database

    class Engine:
        @contextmanager
        def begin(self):
            with connection.begin_nested():
                yield connection

        @contextmanager
        def connect(self):
            yield connection

    monkeypatch.setattr(agencies, "application_engine", lambda: Engine())
    monkeypatch.setattr(agency_applications, "application_engine", lambda: Engine())
    for operation in ["read", "write", "review"]:
        monkeypatch.setattr(
            settings, f"agency_admin_{operation}_groups", ["test-" + operation]
        )
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def login(subject, groups=()):
        from sqlalchemy import text

        from app.db.users import ensure_user

        identity = AuthenticatedUser(
            issuer="https://test.invalid", subject=subject, client_id="test-client"
        )
        local = ensure_user(connection, identity)
        role = (
            3
            if groups
            else (
                2
                if subject == "applicant"
                and connection.scalar(
                    text("SELECT 1 FROM agency_applications WHERE status='approved'")
                )
                else 1
            )
        )
        connection.execute(
            text("UPDATE users SET role_id=:role WHERE id=:id"),
            {"role": role, "id": local.id},
        )
        app.dependency_overrides[require_authenticated_user] = lambda: (
            AuthenticatedUser(
                issuer="https://test.invalid",
                subject=subject,
                client_id="test-client",
                groups=groups,
            )
        )

    login("applicant")
    with TestClient(app) as client:
        created = client.post(
            "/v1/agencies",
            json={
                "contact_name": "Synthetic Contact",
                "contact_email": "contact@example.com",
                "contact_phone": "+923001234567",
                "agency_name": "Synthetic Agency",
            },
        )
        assert created.status_code == 201
        yield client, login, created.json()


def test_review_workflow_and_audit(management):
    client, login, record = management
    agency = record["agency_id"]
    base = f"/v1/admin/agencies/{agency}"
    login("reviewer", ("test-read", "test-write", "test-review"))
    assert (
        client.get(
            "/v1/admin/agencies", params={"application_status": "pending_review"}
        ).json()[0]["agency_id"]
        == agency
    )
    action = {"expected_revision": 1, "reason": "Please clarify trading name"}
    response = client.post(base + "/request-information", json=action)
    assert response.status_code == 200
    assert response.json()["application_status"] == "more_information_required"
    login("applicant")
    update = client.patch(
        "/v1/agencies/me",
        json={
            "contact_name": "Synthetic Contact",
            "contact_email": "contact@example.com",
            "contact_phone": "+923001234567",
            "agency_name": "Clarified Agency",
            "expected_revision": 2,
        },
    )
    assert update.status_code == 200
    assert (
        client.post(
            "/v1/agencies/me/resubmit",
            json={"expected_revision": 3, "reason": "Name clarified"},
        ).status_code
        == 200
    )
    login("reviewer", ("test-review", "test-read"))
    approved = client.post(
        base + "/approve", json={"expected_revision": 4, "reason": "Reviewed manually"}
    )
    assert approved.status_code == 200
    assert approved.json()["agency_status"] == "active"
    assert (
        client.post(
            base + "/approve", json={"expected_revision": 5, "reason": "Again"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            base + "/suspend", json={"expected_revision": 5, "reason": "Access paused"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            base + "/reactivate",
            json={"expected_revision": 6, "reason": "Reviewed again"},
        ).status_code
        == 200
    )
    history = client.get(base + "/history").json()
    assert [row["revision"] for row in history] == [7, 6, 5, 4, 3, 2, 1]
    assert all("actor_subject" not in row for row in history)
    login("applicant")
    assert (
        client.patch(
            "/v1/agencies/me",
            json={
                "contact_name": "Synthetic Contact",
                "contact_email": "contact@example.com",
                "contact_phone": "+923001234567",
                "agency_name": "Changed",
                "expected_revision": 7,
            },
        ).status_code
        == 409
    )
    assert client.get("/v1/agencies/me/history").status_code == 200


def test_permissions_isolation_and_stale_updates(management, monkeypatch):
    client, login, record = management
    base = f"/v1/admin/agencies/{record['agency_id']}"
    assert client.get(base).status_code == 403
    login("foreign")
    assert client.get("/v1/agencies/me").status_code == 404
    login("reviewer", ("test-admin",))
    assert client.get(base).status_code == 200
    assert (
        client.patch(
            base, json={"agency_name": "Changed", "expected_revision": 1}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            base, json={"agency_name": "Stale", "expected_revision": 1}
        ).status_code
        == 409
    )
    login("applicant", ("test-admin",))
    assert (
        client.post(
            base + "/approve", json={"expected_revision": 2, "reason": "Self approval"}
        ).status_code
        == 403
    )
    login("reviewer", ("test-admin",))
    assert (
        client.post(
            base + "/reject", json={"expected_revision": 2, "reason": "Rejected"}
        ).status_code
        == 200
    )
    login("applicant")
    assert (
        client.post(
            "/v1/agencies/me/resubmit",
            json={"expected_revision": 3, "reason": "Try again"},
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "body",
    [
        {"agency_name": "Changed", "expected_revision": 1, "status": "active"},
        {"agency_name": "Changed", "expected_revision": 1, "subject": "foreign"},
        {"agency_name": "Changed"},
    ],
)
def test_no_mass_assignment(management, body):
    client, _, _ = management
    assert client.patch("/v1/agencies/me", json=body).status_code == 422


def test_list_bounds_and_invalid_actions(management):
    client, login, record = management
    login("reviewer", ("test-read", "test-review"))
    assert client.get("/v1/admin/agencies", params={"limit": 101}).status_code == 422
    assert client.get("/v1/admin/agencies", params={"offset": -1}).status_code == 422
    assert (
        client.get(
            "/v1/admin/agencies", params={"application_status": "unknown"}
        ).status_code
        == 422
    )
    base = f"/v1/admin/agencies/{record['agency_id']}"
    assert (
        client.post(
            base + "/suspend", json={"expected_revision": 1, "reason": "Premature"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            base + "/approve", json={"expected_revision": 1, "reason": " "}
        ).status_code
        == 422
    )


def test_audit_failure_rolls_back_review(management, database, monkeypatch):
    from unittest.mock import patch

    from sqlalchemy.exc import OperationalError

    client, login, record = management
    connection, _, _ = database
    execute = connection.execute

    def fail_audit(statement, *args, **kwargs):
        if "INSERT INTO agency_events" in str(statement):
            raise OperationalError("private SQL", {}, Exception("private connection"))
        return execute(statement, *args, **kwargs)

    login("reviewer", ("test-review", "test-read"))
    base = f"/v1/admin/agencies/{record['agency_id']}"
    with patch.object(connection, "execute", side_effect=fail_audit):
        response = client.post(
            base + "/approve", json={"expected_revision": 1, "reason": "Reviewed"}
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Agency service unavailable."}
    current = client.get(base).json()
    assert current["revision"] == 1
    assert current["agency_status"] == "suspended"
    assert current["application_status"] == "pending_review"
    assert len(client.get(base + "/history").json()) == 1


def test_contact_only_update_preserves_name_status_and_other_contacts(management):
    client, login, record = management
    response = client.patch(
        "/v1/agencies/me",
        json={"expected_revision": 1, "contact_phone": "+442079460123"},
    )
    assert response.status_code == 200
    changed = response.json()
    assert changed["agency_name"] == record["agency_name"]
    assert changed["contact_name"] == "Synthetic Contact"
    assert changed["contact_email"] == "contact@example.com"
    assert changed["contact_phone"] == "+442079460123"
    assert changed["application_status"] == "pending_review"
    assert changed["agency_status"] == "suspended"
    assert changed["revision"] == 2
    login("reviewer", ("test-read", "test-write"))
    base = f"/v1/admin/agencies/{record['agency_id']}"
    response = client.patch(
        base, json={"expected_revision": 2, "contact_email": "office@example.com"}
    )
    assert response.status_code == 200
    assert response.json()["contact_phone"] == "+442079460123"
    assert response.json()["contact_email"] == "office@example.com"
    assert (
        client.get("/v1/admin/agencies").json()[0]["contact_email"]
        == "office@example.com"
    )
    login("foreign")
    assert client.get("/v1/agencies/me").status_code == 404


def test_contact_update_obeys_approved_profile_restriction(management):
    client, login, record = management
    login("reviewer", ("test-review",))
    assert (
        client.post(
            f"/v1/admin/agencies/{record['agency_id']}/approve",
            json={"expected_revision": 1, "reason": "Reviewed"},
        ).status_code
        == 200
    )
    login("applicant")
    assert (
        client.patch(
            "/v1/agencies/me",
            json={"expected_revision": 2, "contact_phone": "+442079460123"},
        ).status_code
        == 409
    )
