from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.db import agency_integrations as store


@pytest.fixture
def registry(database, monkeypatch):
    connection, metadata, _ = database

    class Engine:
        @contextmanager
        def begin(self):
            with connection.begin_nested():
                yield connection

    monkeypatch.setattr(store, "application_engine", lambda: Engine())
    connection.execute(
        metadata.tables["agencies"]
        .insert()
        .values(id="test-agency", name="Synthetic", status="active")
    )
    connection.execute(
        metadata.tables["users"]
        .insert()
        .values(
            id="11111111-1111-4111-8111-111111111111",
            cognito_issuer="https://test.invalid",
            cognito_sub="test-owner",
        )
    )
    connection.execute(
        metadata.tables["agency_cognito_assignments"]
        .insert()
        .values(
            user_id="11111111-1111-4111-8111-111111111111",
            agency_id="test-agency",
            issuer="https://test.invalid",
            subject="test-owner",
        )
    )
    connection.execute(
        metadata.tables["agency_applications"]
        .insert()
        .values(agency_id="test-agency", status="approved")
    )
    return connection


def test_registry_registration_scope_revocation_and_duplicates(registry):
    record = store.register(
        "https://test.invalid", "testmachineclient", "test-agency", ["test-api/search"]
    )
    assert record["enabled"] is True
    assert (
        store.lookup("https://test.invalid", "testmachineclient")["agency_id"]
        == "test-agency"
    )
    assert (
        len(store.list_integrations("https://test.invalid", "test-agency", 20, 0)) == 1
    )
    with pytest.raises(HTTPException) as error:
        store.register(
            "https://test.invalid",
            "testmachineclient",
            "test-agency",
            ["test-api/book"],
        )
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        store.lookup("https://different.invalid", "testmachineclient")
    store.revoke("https://test.invalid", "testmachineclient")
    with pytest.raises(HTTPException) as error:
        store.lookup("https://test.invalid", "testmachineclient")
    assert error.value.status_code == 403
    assert (
        store.list_integrations("https://test.invalid", "test-agency", 20, 0)[0][
            "enabled"
        ]
        is False
    )


def test_suspended_or_unapproved_agency_is_denied(registry):
    store.register(
        "https://test.invalid", "testmachineclient", "test-agency", ["test-api/search"]
    )
    registry.execute(text("UPDATE agencies SET status='suspended'"))
    with pytest.raises(HTTPException):
        store.lookup("https://test.invalid", "testmachineclient")
    with pytest.raises(HTTPException) as error:
        store.register(
            "https://test.invalid", "anotherclient", "test-agency", ["test-api/search"]
        )
    assert error.value.status_code == 409
    registry.execute(text("UPDATE agencies SET status='active'"))
    registry.execute(text("UPDATE agency_applications SET status='pending_review'"))
    with pytest.raises(HTTPException):
        store.lookup("https://test.invalid", "testmachineclient")
