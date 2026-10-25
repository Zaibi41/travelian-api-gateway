"""Registry reads are authoritative on every request; never store secrets."""

from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db.agency_applications import application_engine


@contextmanager
def transaction():
    try:
        with application_engine().begin() as connection:
            yield connection
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise HTTPException(409, "Integration already registered.") from None
        raise HTTPException(503, "Integration service unavailable.") from None
    except SQLAlchemyError:
        raise HTTPException(503, "Integration service unavailable.") from None


def lookup(issuer, client_id):
    with transaction() as connection:
        row = (
            connection.execute(
                text("""
            SELECT i.agency_id, i.scopes FROM agency_integrations i
            JOIN agencies a ON a.id=i.agency_id
            JOIN agency_applications p ON p.agency_id=a.id
            WHERE i.issuer=:issuer AND i.client_id=:client
              AND i.enabled=true AND a.status='active' AND p.status='approved'
        """),
                {"issuer": issuer, "client": client_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(403, "Integration access denied.")
        return dict(row)


def register(issuer, client_id, agency_id, scopes):
    with transaction() as connection:
        if (
            connection.scalar(
                text(
                    "SELECT 1 FROM agencies a JOIN agency_applications p ON p.agency_id=a.id WHERE a.id=:id AND a.status='active' AND p.status='approved' FOR UPDATE OF a, p"
                ),
                {"id": agency_id},
            )
            is None
        ):
            raise HTTPException(409, "Integration requires an approved active agency.")
        connection.execute(
            text(
                "INSERT INTO agency_integrations (issuer, client_id, agency_id, scopes, enabled) VALUES (:issuer,:client,:agency,:scopes,true)"
            ),
            {
                "issuer": issuer,
                "client": client_id,
                "agency": agency_id,
                "scopes": scopes,
            },
        )
        return {
            "client_id": client_id,
            "agency_id": agency_id,
            "enabled": True,
            "scopes": scopes,
        }


def list_integrations(issuer, agency_id, limit, offset):
    with transaction() as connection:
        return (
            connection.execute(
                text(
                    "SELECT client_id, agency_id, enabled, scopes FROM agency_integrations WHERE issuer=:issuer AND agency_id=:agency ORDER BY client_id LIMIT :limit OFFSET :offset"
                ),
                {
                    "issuer": issuer,
                    "agency": agency_id,
                    "limit": limit,
                    "offset": offset,
                },
            )
            .mappings()
            .all()
        )


def revoke(issuer, client_id):
    with transaction() as connection:
        result = connection.execute(
            text(
                "UPDATE agency_integrations SET enabled=false WHERE issuer=:issuer AND client_id=:client RETURNING client_id"
            ),
            {"issuer": issuer, "client": client_id},
        ).scalar_one_or_none()
        if result is None:
            raise HTTPException(404, "Integration not found.")
