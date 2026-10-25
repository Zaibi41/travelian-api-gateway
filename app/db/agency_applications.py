"""Transactional self-registration, with identity from verified tokens only."""

from functools import lru_cache
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Connection, create_engine, pool, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.authentication import AuthenticatedUser
from app.db.config import DatabaseSettings
from app.schemas.agency_applications import (
    AgencyApplicationRequest,
    AgencyApplicationResponse,
)


@lru_cache(maxsize=1)
def application_engine():
    settings = DatabaseSettings()
    if not settings.db_password:
        raise HTTPException(503, "Agency applications unavailable.")
    return create_engine(
        settings.url(),
        poolclass=pool.NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 3,
            "options": "-c statement_timeout=3000 -c lock_timeout=3000",
        },
    )


def find_application(connection: Connection, user: AuthenticatedUser):
    row = (
        connection.execute(
            text("""
        SELECT a.id AS agency_id, a.name AS agency_name,
               application.status AS application_status, a.status AS agency_status,
               application.submitted_at, a.revision, a.contact_name, a.contact_email, a.contact_phone
        FROM agency_cognito_assignments AS assignment
        JOIN agencies AS a ON a.id = assignment.agency_id
        JOIN agency_applications AS application ON application.agency_id = a.id
        WHERE assignment.issuer = :issuer AND assignment.subject = :subject
    """),
            {"issuer": user.issuer, "subject": user.subject},
        )
        .mappings()
        .one_or_none()
    )
    return AgencyApplicationResponse.model_validate(dict(row)) if row else None


def insert_application(
    connection: Connection, user: AuthenticatedUser, body: AgencyApplicationRequest
):
    from app.db.users import ensure_user

    local_user = ensure_user(connection, user)
    agency_id = str(uuid4())
    connection.execute(
        text(
            "INSERT INTO agencies (id, name, contact_name, contact_email, contact_phone) VALUES (:id, :agency_name, :contact_name, :contact_email, :contact_phone)"
        ),
        {"id": agency_id, **body.model_dump()},
    )
    # Unique (issuer, subject) serializes competing registrations. Any failure
    # rolls back the agency insert as well; no orphan or automatic retry.
    connection.execute(
        text(
            "INSERT INTO agency_cognito_assignments (agency_id, issuer, subject, user_id) VALUES (:id, :issuer, :subject, :user_id)"
        ),
        {
            "id": agency_id,
            "issuer": user.issuer,
            "subject": user.subject,
            "user_id": local_user.id,
        },
    )
    connection.execute(
        text("INSERT INTO agency_applications (agency_id) VALUES (:id)"),
        {"id": agency_id},
    )
    connection.execute(
        text(
            "INSERT INTO agency_events (agency_id, action, actor_issuer, actor_subject, revision) VALUES (:id, 'submitted', :issuer, :subject, 1)"
        ),
        {"id": agency_id, "issuer": user.issuer, "subject": user.subject},
    )
    return find_application(connection, user)


def create_application(user: AuthenticatedUser, body: AgencyApplicationRequest):
    try:
        with application_engine().begin() as connection:
            return insert_application(connection, user, body)
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise HTTPException(
                409, "An agency assignment or application already exists."
            ) from None
        raise HTTPException(503, "Agency applications unavailable.") from None
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(503, "Agency applications unavailable.") from None


def get_application(user: AuthenticatedUser):
    from app.db.users import ensure_user

    try:
        with application_engine().begin() as connection:
            ensure_user(connection, user)
            result = find_application(connection, user)
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(503, "Agency applications unavailable.") from None
    if result is None:
        raise HTTPException(404, "Agency application not found.")
    return result
