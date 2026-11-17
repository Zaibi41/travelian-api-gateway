"""Read-only assignment resolution from the authoritative local store."""

from functools import lru_cache

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Connection, create_engine, pool, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.authorization import AgencyAuthorization
from app.db.config import DatabaseSettings

_ASSIGNMENT = text("""
    SELECT a.id
    FROM agencies AS a
    JOIN agency_cognito_assignments AS membership ON membership.agency_id = a.id
    WHERE membership.issuer = :issuer AND membership.subject = :subject
      AND a.status = 'active'
""")


def check_active_assignment(
    connection: Connection, principal: AgencyAuthorization
) -> AgencyAuthorization:
    agency_id = connection.execute(
        _ASSIGNMENT, {"issuer": principal.issuer, "subject": principal.subject}
    ).scalar_one_or_none()
    if agency_id is None or agency_id != principal.agency_id:
        raise HTTPException(status_code=403, detail="Booking access denied.")
    return principal


@lru_cache(maxsize=1)
def assignment_engine():
    settings = DatabaseSettings()
    # Runtime must never prompt or fall back to implicit local OS credentials.
    if not settings.db_password:
        raise HTTPException(status_code=503, detail="Agency verification unavailable.")
    return create_engine(
        settings.url(),
        poolclass=pool.NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 3,
            "options": "-c statement_timeout=3000 -c default_transaction_read_only=on",
        },
    )


def require_active_assignment(principal: AgencyAuthorization) -> AgencyAuthorization:
    try:
        with assignment_engine().connect() as connection:
            return check_active_assignment(connection, principal)
    except (SQLAlchemyError, ValidationError):
        # Do not return/log SQL, connection details, identity or driver exceptions.
        raise HTTPException(
            status_code=503, detail="Agency verification unavailable."
        ) from None
