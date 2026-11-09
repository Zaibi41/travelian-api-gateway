"""Real PostgreSQL constraints when an explicitly isolated test URL is provided."""

from io import StringIO

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from app.db.config import DatabaseSettings


def test_offline_migration_can_be_reviewed_without_credentials():
    output = StringIO()
    command.upgrade(Config("alembic.ini", output_buffer=output), "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE agencies" in sql
    assert "uq_agency_cognito_identity" in sql
    assert "suspended" in sql


def test_database_password_is_hidden_and_special_characters_are_preserved():
    settings = DatabaseSettings(
        _env_file=None,
        db_host="127.0.0.1",
        db_port=5432,
        db_name="synthetic",
        db_user="synthetic",
        db_password="test@:/%password",
    )
    assert "test@:/%password" not in repr(settings)
    assert "test@:/%password" not in str(settings.url())
    assert settings.url().password == "test@:/%password"


def test_agency_constraints_and_migration_round_trip(database):
    connection, metadata, config = database
    agencies = metadata.tables["agencies"]
    assignments = metadata.tables["agency_cognito_assignments"]
    connection.execute(
        metadata.tables["users"]
        .insert()
        .values(
            id="11111111-1111-4111-8111-111111111111",
            cognito_issuer="https://test.invalid/pool",
            cognito_sub="synthetic-user",
        )
    )
    connection.execute(
        agencies.insert(),
        [{"id": "test-a", "name": "Agency A"}, {"id": "test-b", "name": "Agency B"}],
    )
    assert (
        connection.scalar(sa.select(agencies.c.status).where(agencies.c.id == "test-a"))
        == "suspended"
    )
    identity = {
        "user_id": "11111111-1111-4111-8111-111111111111",
        "agency_id": "test-a",
        "issuer": "https://test.invalid/pool",
        "subject": "synthetic-user",
    }
    connection.execute(assignments.insert().values(**identity))
    invalid = [
        assignments.insert().values(**{**identity, "subject": "other-user"}),
        assignments.insert().values(**{**identity, "agency_id": "test-b"}),
        assignments.insert().values(
            **{**identity, "agency_id": "missing", "subject": "another"}
        ),
        assignments.insert().values(agency_id="test-b", issuer=" ", subject="someone"),
        assignments.insert().values(
            agency_id="test-b", issuer="https://test.invalid/pool", subject=""
        ),
        agencies.insert().values(id="test-c", name="", status="active"),
        agencies.insert().values(id="test-c", name="Agency C", status="unknown"),
        agencies.delete().where(agencies.c.id == "test-a"),
    ]
    for statement in invalid:
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(statement)
    command.downgrade(config, "base")
    assert "agencies" not in sa.inspect(connection).get_table_names()
    command.upgrade(config, "head")
    assert "agency_cognito_assignments" in sa.inspect(connection).get_table_names()


def test_active_assignment_requires_exact_identity_agency_and_status(database):
    from fastapi import HTTPException

    from app.core.authorization import AgencyAuthorization, BookingPermission
    from app.db.agency_assignments import check_active_assignment

    connection, metadata, _ = database
    agencies = metadata.tables["agencies"]
    assignments = metadata.tables["agency_cognito_assignments"]
    connection.execute(
        metadata.tables["users"]
        .insert()
        .values(
            id="11111111-1111-4111-8111-111111111111",
            cognito_issuer="https://test.invalid/pool",
            cognito_sub="synthetic-user",
        )
    )
    connection.execute(
        agencies.insert().values(id="test-a", name="Synthetic agency", status="active")
    )
    connection.execute(
        assignments.insert().values(
            user_id="11111111-1111-4111-8111-111111111111",
            agency_id="test-a",
            issuer="https://test.invalid/pool",
            subject="synthetic-user",
        )
    )
    principal = AgencyAuthorization(
        issuer="https://test.invalid/pool",
        subject="synthetic-user",
        agency_id="test-a",
        permission=BookingPermission.CREATE,
    )
    assert check_active_assignment(connection, principal) == principal
    for updates in [
        {"issuer": "https://other.invalid/pool"},
        {"subject": "foreign-user"},
        {"agency_id": "test-b"},
        {"subject": "x' OR 1=1 --"},
    ]:
        with pytest.raises(HTTPException) as denied:
            check_active_assignment(connection, principal.model_copy(update=updates))
        assert denied.value.status_code == 403
    connection.execute(agencies.update().values(status="suspended"))
    with pytest.raises(HTTPException) as denied:
        check_active_assignment(connection, principal)
    assert denied.value.status_code == 403
