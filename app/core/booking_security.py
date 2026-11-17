"""Authenticate and check agency policy, then deny until ownership is integrated."""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from starlette.concurrency import run_in_threadpool

from app.core.authentication import (
    AuthenticatedMachine,
    AuthenticatedUser,
    bearer,
    require_authenticated_user,
)
from app.core.authorization import (
    BookingPermission,
    policy_unavailable,
)
from app.core.config import settings
from app.core.integration_security import authorize_machine, require_machine
from app.db.agency_assignments import require_active_assignment
from app.db.users import authorize_agency_operation


async def require_booking_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    # Choose the human verifier only for its exact configured app client. The
    # machine verifier always rechecks signatures and registry membership.
    import jwt

    if credentials is not None and len(credentials.credentials) <= 16384:
        try:
            hint = jwt.decode(
                credentials.credentials, options={"verify_signature": False}
            )
            if hint.get("client_id") != settings.cognito_app_client_id:
                return await require_machine(request, credentials)
        except (jwt.InvalidTokenError, ValueError, TypeError):
            pass
    return await require_authenticated_user(request, credentials)


async def require_booking_authorization(
    request: Request,
    user: Annotated[
        AuthenticatedUser | AuthenticatedMachine, Depends(require_booking_identity)
    ],
) -> None:
    # Match server-controlled operation names; never trust a requested permission.
    permission = {
        "create_group_booking": BookingPermission.CREATE,
        "fetch_booking_details": BookingPermission.DETAILS,
    }.get(getattr(request.scope.get("route"), "name", None))
    if permission is None:
        raise policy_unavailable()
    if isinstance(user, AuthenticatedMachine):
        scope = (
            settings.integration_booking_create_scope
            if permission == BookingPermission.CREATE
            else settings.integration_booking_details_scope
        )
        await authorize_machine(user, scope)
        # Machine booking ownership/idempotency is not implemented yet.
        raise policy_unavailable()
    principal = await run_in_threadpool(authorize_agency_operation, user, permission)
    await run_in_threadpool(require_active_assignment, principal)
    # Active agency assignment does not establish booking ownership
    # or duplicate protection. Never dispatch a supplier request yet.
    raise policy_unavailable()
