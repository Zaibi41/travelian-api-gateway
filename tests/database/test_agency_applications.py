from contextlib import contextmanager
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app.api.routes.agency_applications import router
from app.core.authentication import AuthenticatedUser, require_authenticated_user
from app.db import agency_applications as store
from app.schemas.agency_applications import AgencyApplicationRequest


def user(subject="synthetic-applicant"):
    return AuthenticatedUser(
        issuer="https://test.invalid/pool", subject=subject, client_id="test-client"
    )


@pytest.fixture
def application_client(database, monkeypatch):
    connection, _, _ = database

    class Engine:
        @contextmanager
        def begin(self):
            with connection.begin_nested():
                yield connection

        @contextmanager
        def connect(self):
            yield connection

    monkeypatch.setattr(store, "application_engine", lambda: Engine())
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_authenticated_user] = lambda: user()
    with TestClient(app) as client:
        yield client, app, connection


def test_application_creation_status_and_duplicate_rollback(application_client):
    client, app, connection = application_client
    assert client.get("/v1/agencies/applications/me").status_code == 404
    created = client.post(
        "/v1/agencies/applications",
        json={
            "contact_name": "Synthetic Contact",
            "contact_email": "contact@example.com",
            "contact_phone": "+923001234567",
            "agency_name": "  Synthetic Agency  ",
        },
    )
    assert created.status_code == 201
    result = created.json()
    assert str(UUID(result["agency_id"])) == result["agency_id"]
    assert UUID(result["agency_id"]).version == 4
    assert result["agency_name"] == "Synthetic Agency"
    assert result["application_status"] == "pending_review"
    assert result["agency_status"] == "suspended"
    assert "subject" not in result and "issuer" not in result
    assert client.get("/v1/agencies/applications/me").json() == result
    duplicate = client.post(
        "/v1/agencies/applications",
        json={
            "contact_name": "Synthetic Contact",
            "contact_email": "contact@example.com",
            "contact_phone": "+923001234567",
            "agency_name": "Duplicate",
        },
    )
    assert duplicate.status_code == 409
    assert connection.scalar(sa.text("SELECT count(*) FROM agencies")) == 1
    assert connection.scalar(sa.text("SELECT count(*) FROM agency_applications")) == 1
    app.dependency_overrides[require_authenticated_user] = lambda: user("other-user")
    assert (
        client.get(
            "/v1/agencies/applications/me", params={"agency_id": result["agency_id"]}
        ).status_code
        == 404
    )
    second = client.post(
        "/v1/agencies/applications",
        json={
            "contact_name": "Synthetic Contact",
            "contact_email": "contact@example.com",
            "contact_phone": "+923001234567",
            "agency_name": "Other Agency",
        },
    )
    assert second.status_code == 201
    assert second.json()["agency_id"] != result["agency_id"]


@pytest.mark.parametrize(
    "field", ["subject", "issuer", "agency_id", "status", "groups", "agency_status"]
)
def test_caller_cannot_assign_identity_or_approve(application_client, field):
    client, _, connection = application_client
    response = client.post(
        "/v1/agencies/applications",
        json={
            "contact_name": "Synthetic Contact",
            "contact_email": "contact@example.com",
            "contact_phone": "+923001234567",
            "agency_name": "Synthetic Agency",
            field: "forged",
        },
    )
    assert response.status_code == 422
    assert connection.scalar(sa.text("SELECT count(*) FROM agencies")) == 0


@pytest.mark.parametrize(
    "name", ["", "   ", "a" * 201, "Agency\nName", "Agency\x00Name", 123, None]
)
def test_invalid_names(name):
    with pytest.raises(ValidationError):
        AgencyApplicationRequest(
            agency_name=name,
            contact_name="Synthetic Contact",
            contact_email="contact@example.com",
            contact_phone="+923001234567",
        )


@pytest.mark.parametrize(
    "method,path",
    [("post", "/v1/agencies/applications"), ("get", "/v1/agencies/applications/me")],
)
def test_unauthenticated_calls_do_not_reach_store(monkeypatch, method, path):
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def deny():
        raise HTTPException(401, "Invalid or missing access token.")

    app.dependency_overrides[require_authenticated_user] = deny
    engine = Mock()
    monkeypatch.setattr(store, "application_engine", engine)
    with TestClient(app) as client:
        response = getattr(client, method)(path)
    assert response.status_code == 401
    engine.assert_not_called()


def test_partial_failure_rolls_back_all_records(application_client, monkeypatch):
    client, _, connection = application_client
    monkeypatch.setattr(
        store,
        "find_application",
        Mock(
            side_effect=OperationalError(
                "private SQL", {}, Exception("private connection")
            )
        ),
    )
    response = client.post(
        "/v1/agencies/applications",
        json={
            "contact_name": "Synthetic Contact",
            "contact_email": "contact@example.com",
            "contact_phone": "+923001234567",
            "agency_name": "Synthetic Agency",
        },
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Agency applications unavailable."}
    for table in ["agencies", "agency_cognito_assignments", "agency_applications"]:
        assert connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) == 0


def test_missing_configuration_and_database_outage(monkeypatch):
    from app.db.config import DatabaseSettings

    store.application_engine.cache_clear()
    monkeypatch.setattr(
        store,
        "DatabaseSettings",
        lambda: DatabaseSettings(
            _env_file=None,
            db_host="127.0.0.1",
            db_port=5432,
            db_name="synthetic",
            db_user="synthetic",
            db_password=None,
        ),
    )
    with pytest.raises(HTTPException) as error:
        store.get_application(user())
    assert error.value.status_code == 503
    engine = Mock()
    engine.begin.side_effect = OperationalError(
        "private SQL", {}, Exception("private connection")
    )
    monkeypatch.setattr(store, "application_engine", lambda: engine)
    with pytest.raises(HTTPException) as error:
        store.get_application(user())
    assert error.value.detail == "Agency applications unavailable."
