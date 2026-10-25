import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.authentication import AuthenticatedUser
from app.db.users import ensure_user, local_identity, require_admin


def identity(subject="synthetic-user", issuer="https://test.invalid", groups=()):
    return AuthenticatedUser(
        subject=subject, issuer=issuer, client_id="testclient", groups=groups
    )


def test_verified_profile_is_unique_and_groups_cannot_grant_admin(database):
    connection, _, _ = database
    user = identity(groups=("super_admin", "admin"))
    first = ensure_user(connection, user)
    assert first.role_id == 1 and first.role == "customer"
    assert ensure_user(connection, user).id == first.id
    assert (
        ensure_user(connection, identity(issuer="https://other.invalid")).id != first.id
    )
    with pytest.raises(HTTPException) as denied:
        require_admin(user)
    assert denied.value.status_code == 403
    connection.execute(
        text("UPDATE users SET role_id=3 WHERE id=:id"), {"id": first.id}
    )
    assert require_admin(identity()).subject == user.subject
    connection.execute(
        text("UPDATE users SET role_id=1 WHERE id=:id"), {"id": first.id}
    )
    with pytest.raises(HTTPException):
        require_admin(user)
    connection.execute(
        text("UPDATE users SET active=false WHERE id=:id"), {"id": first.id}
    )
    with pytest.raises(HTTPException):
        local_identity(user)


def test_customer_identity_has_no_agency(database):
    result = local_identity(identity())
    assert result["role"] == "customer"
    assert result["agency_id"] is None
    assert "groups" not in result


def test_migration_backfills_approved_owner_without_elevating_admin(database):
    from alembic import command

    connection, _, config = database
    command.downgrade(config, "0005_agency_contacts")
    connection.execute(
        text(
            "INSERT INTO agencies (id,name,status) VALUES ('agency-legacy','Legacy','suspended')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO agency_cognito_assignments (agency_id,issuer,subject) VALUES ('agency-legacy','https://test.invalid','legacy-owner')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO agency_applications (agency_id,status) VALUES ('agency-legacy','approved')"
        )
    )
    command.upgrade(config, "head")
    row = (
        connection.execute(
            text(
                "SELECT u.role_id,x.agency_id,u.id FROM users u JOIN agency_cognito_assignments x ON x.user_id=u.id"
            )
        )
        .mappings()
        .one()
    )
    assert row["role_id"] == 2
    assert row["agency_id"] == "agency-legacy"
    assert connection.scalar(text("SELECT count(*) FROM users WHERE role_id=3")) == 0


def test_registration_approval_and_suspension_use_database_role(database, monkeypatch):
    from contextlib import contextmanager

    from app.core.authorization import BookingPermission
    from app.db import agency_applications
    from app.db.agencies import change
    from app.db.users import authorize_agency_operation
    from app.schemas.agencies import AgencyAction
    from app.schemas.agency_applications import AgencyApplicationRequest

    connection, _, _ = database

    class Engine:
        @contextmanager
        def begin(self):
            with connection.begin_nested():
                yield connection

    monkeypatch.setattr(agency_applications, "application_engine", lambda: Engine())
    applicant = identity("owner", groups=("super_admin",))
    record = agency_applications.create_application(
        applicant,
        AgencyApplicationRequest(
            agency_name="Synthetic",
            contact_name="Person",
            contact_email="office@example.com",
            contact_phone="+923001234567",
        ),
    )
    assert local_identity(applicant)["role"] == "customer"
    reviewer = identity("reviewer")
    profile = ensure_user(connection, reviewer)
    connection.execute(
        text("UPDATE users SET role_id=3 WHERE id=:id"), {"id": profile.id}
    )
    change(
        reviewer,
        AgencyAction(expected_revision=1, reason="Reviewed"),
        "approved",
        record.agency_id,
    )
    assert local_identity(applicant)["role"] == "agency_owner"
    assert (
        connection.scalar(
            text("SELECT count(*) FROM user_role_events WHERE new_role_id=2")
        )
        == 1
    )
    assert (
        authorize_agency_operation(applicant, BookingPermission.CREATE).agency_id
        == record.agency_id
    )
    with pytest.raises(HTTPException):
        require_admin(applicant)
    change(
        reviewer,
        AgencyAction(expected_revision=2, reason="Paused"),
        "suspended",
        record.agency_id,
    )
    assert local_identity(applicant)["role"] == "agency_owner"
    with pytest.raises(HTTPException):
        authorize_agency_operation(applicant, BookingPermission.CREATE)


def test_operator_role_assignment_is_audited(database, monkeypatch):
    from app.cli.set_user_role import main

    connection, _, _ = database
    profile = ensure_user(connection, identity())
    monkeypatch.setattr(
        "sys.argv",
        [
            "set_user_role",
            "--user-id",
            profile.id,
            "--role",
            "super_admin",
            "--reason",
            "Local bootstrap test",
        ],
    )
    main()
    assert require_admin(identity()).subject == "synthetic-user"
    assert (
        connection.scalar(
            text(
                "SELECT count(*) FROM user_role_events WHERE user_id=:id AND new_role_id=3"
            ),
            {"id": profile.id},
        )
        == 1
    )
