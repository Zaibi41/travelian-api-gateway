"""PostgreSQL is the role authority. Cognito groups never elevate local users."""

from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.db.agencies import transaction


class UserProfile(BaseModel):
    id: str
    role_id: int
    role: str
    active: bool


def ensure_user(connection, identity):
    connection.execute(
        text(
            "INSERT INTO users (id,cognito_issuer,cognito_sub) VALUES (:id,:issuer,:subject) ON CONFLICT (cognito_issuer,cognito_sub) DO NOTHING"
        ),
        {"id": str(uuid4()), "issuer": identity.issuer, "subject": identity.subject},
    )
    row = (
        connection.execute(
            text(
                "SELECT u.id,u.role_id,r.name AS role,u.active FROM users u JOIN roles r ON r.id=u.role_id WHERE cognito_issuer=:issuer AND cognito_sub=:subject"
            ),
            {"issuer": identity.issuer, "subject": identity.subject},
        )
        .mappings()
        .one()
    )
    profile = UserProfile.model_validate(dict(row))
    if not profile.active:
        raise HTTPException(403, "User account is inactive.")
    return profile


def profile(identity):
    with transaction() as connection:
        return ensure_user(connection, identity)


def require_admin(identity):
    result = profile(identity)
    if result.role != "super_admin":
        raise HTTPException(403, "Agency administration access denied.")
    return identity


def local_identity(identity):
    with transaction() as connection:
        local = ensure_user(connection, identity)
        agency = (
            connection.execute(
                text(
                    "SELECT a.id AS agency_id,a.status AS agency_status,p.status AS application_status FROM agency_cognito_assignments x JOIN agencies a ON a.id=x.agency_id LEFT JOIN agency_applications p ON p.agency_id=a.id WHERE x.user_id=:id"
                ),
                {"id": local.id},
            )
            .mappings()
            .one_or_none()
        )
        return {
            "subject": identity.subject,
            "issuer": identity.issuer,
            "client_id": identity.client_id,
            "user_id": local.id,
            "role_id": local.role_id,
            "role": local.role,
            **(
                dict(agency)
                if agency
                else {
                    "agency_id": None,
                    "agency_status": None,
                    "application_status": None,
                }
            ),
        }


def authorize_agency_operation(identity, permission):
    from app.core.authorization import AgencyAuthorization

    with transaction() as connection:
        local = ensure_user(connection, identity)
        if local.role != "agency_owner":
            raise HTTPException(403, "Booking access denied.")
        agency_id = connection.scalar(
            text(
                "SELECT a.id FROM agency_cognito_assignments x JOIN agencies a ON a.id=x.agency_id JOIN agency_applications p ON p.agency_id=a.id WHERE x.user_id=:id AND a.status='active' AND p.status='approved'"
            ),
            {"id": local.id},
        )
        if not agency_id:
            raise HTTPException(403, "Booking access denied.")
        return AgencyAuthorization(
            issuer=identity.issuer,
            subject=identity.subject,
            agency_id=agency_id,
            permission=permission,
        )
