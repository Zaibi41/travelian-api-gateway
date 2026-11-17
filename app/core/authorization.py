"""Legacy claim-policy helpers retained for compatibility tests only.

Live human booking policy now uses app.db.users and PostgreSQL roles.
Agency-scoped policy from verified Cognito claims, not caller headers/body.

This checks an agency entitlement, not unique agency assignment or booking
ownership. Those require an authoritative record before dispatch is enabled.
"""

import re
from enum import StrEnum

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from app.core.authentication import AuthenticatedUser
from app.core.config import settings


class BookingPermission(StrEnum):
    CREATE = "bookings:create"
    DETAILS = "bookings:details"


class AgencyAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)
    issuer: str
    subject: str
    agency_id: str
    permission: BookingPermission


def policy_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503, detail="Booking authorization is not configured."
    )


def authorize_agency_operation(
    user: AuthenticatedUser,
    permission: BookingPermission,
) -> AgencyAuthorization:
    claim = settings.cognito_agency_claim
    allowed = {
        BookingPermission.CREATE: settings.booking_create_groups,
        BookingPermission.DETAILS: settings.booking_details_groups,
    }.get(permission)
    # Custom namespace prevents accidentally treating sub/email as agency ID.
    if (
        not claim
        or not re.fullmatch(r"custom:[A-Za-z0-9_-]{1,64}", claim)
        or not allowed
        or any(not group or group != group.strip() for group in allowed)
    ):
        raise policy_unavailable()
    if (
        user.agency_claim != claim
        or not user.agency_id
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", user.agency_id)
        or not set(user.groups).intersection(allowed)
    ):
        # No implicit admin, staff, email-domain or parent-agency override.
        raise HTTPException(status_code=403, detail="Booking access denied.")
    return AgencyAuthorization(
        issuer=user.issuer,
        subject=user.subject,
        agency_id=user.agency_id,
        permission=permission,
    )


class BookingOwner(BaseModel):
    """Ownership from a future trusted repository, never a caller payload."""

    model_config = ConfigDict(frozen=True)
    issuer: str
    subject: str
    agency_id: str


def authorize_owned_booking(
    principal: AgencyAuthorization,
    owner: BookingOwner | None,
) -> None:
    if principal.permission != BookingPermission.DETAILS:
        raise HTTPException(status_code=403, detail="Booking access denied.")
    if owner is None or (principal.issuer, principal.subject, principal.agency_id) != (
        owner.issuer,
        owner.subject,
        owner.agency_id,
    ):
        # Match the same denial for unknown and other-tenant records.
        raise HTTPException(status_code=404, detail="Booking not found.")
