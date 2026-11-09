from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.core.authorization import AgencyAuthorization, BookingPermission
from app.db.agency_assignments import assignment_engine, require_active_assignment
from app.db.config import DatabaseSettings


def test_missing_runtime_password_fails_closed_without_connecting():
    assignment_engine.cache_clear()
    with (
        patch(
            "app.db.agency_assignments.DatabaseSettings",
            return_value=DatabaseSettings(
                _env_file=None,
                db_host="127.0.0.1",
                db_port=5432,
                db_name="synthetic",
                db_user="synthetic",
                db_password=None,
            ),
        ),
        patch("app.db.agency_assignments.create_engine") as create,
        pytest.raises(HTTPException) as error,
    ):
        assignment_engine()
    assert error.value.status_code == 503
    create.assert_not_called()
    assignment_engine.cache_clear()


def test_database_failure_is_redacted():
    principal = AgencyAuthorization(
        issuer="https://test.invalid",
        subject="private-subject",
        agency_id="test-a",
        permission=BookingPermission.CREATE,
    )
    engine = Mock()
    engine.connect.side_effect = OperationalError(
        "private SQL", {}, Exception("private connection")
    )
    with (
        patch("app.db.agency_assignments.assignment_engine", return_value=engine),
        pytest.raises(HTTPException) as error,
    ):
        require_active_assignment(principal)
    assert error.value.status_code == 503
    assert error.value.detail == "Agency verification unavailable."


@pytest.mark.parametrize("database_status", [None, 403, 503])
def test_booking_guard_never_enables_dispatch(database_status):
    import asyncio
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.core.booking_security import require_booking_authorization

    principal = AgencyAuthorization(
        issuer="https://test.invalid",
        subject="synthetic-user",
        agency_id="test-a",
        permission=BookingPermission.CREATE,
    )
    lookup = Mock(return_value=principal)
    if database_status:
        lookup.side_effect = HTTPException(database_status, "Denied")
    request = Request(
        {"type": "http", "route": SimpleNamespace(name="create_group_booking")}
    )
    with (
        patch(
            "app.core.booking_security.authorize_agency_operation",
            return_value=principal,
        ),
        patch("app.core.booking_security.require_active_assignment", lookup),
        pytest.raises(HTTPException) as error,
    ):
        asyncio.run(require_booking_authorization(request, Mock()))
    assert error.value.status_code == (database_status or 503)
    lookup.assert_called_once_with(principal)
