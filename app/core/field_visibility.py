"""Field visibility for back-office responses (spec v3.0 D23, AGENTS.md).

The gateway decides what each role receives; the page never hides data as a
protection. Sensitive fields are left OUT of the response (not sent as null):

- super_admin: supplier/provider names, supplier cost and margin.
- staff: neither by default. Grantable per role or per user:
  "see_supplier_names" and "see_cost_and_margin".
- agency_owner, customer (and any other role): never, and grants are ignored.

Use `require_back_office` on admin endpoints: it admits super_admin and staff
(agency and customer roles get 403) and returns the caller's visibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.core.authentication import AuthenticatedUser, require_authenticated_user

SEE_SUPPLIER_NAMES = "see_supplier_names"
SEE_COST_AND_MARGIN = "see_cost_and_margin"
PERMISSIONS = (SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN)

FULL_ACCESS_ROLES = {"super_admin"}
GRANTABLE_ROLES = {"staff"}
BACK_OFFICE_ROLES = FULL_ACCESS_ROLES | GRANTABLE_ROLES
#: Roles that may search supplier inventory (Group Ticketing, flights).
SEARCH_ROLES = BACK_OFFICE_ROLES | {"agency_owner"}


@dataclass(frozen=True)
class FieldVisibility:
    supplier_names: bool
    cost_and_margin: bool


NONE = FieldVisibility(supplier_names=False, cost_and_margin=False)
ALL = FieldVisibility(supplier_names=True, cost_and_margin=True)


def resolve_visibility(role: str, granted: set[str] | frozenset[str]) -> FieldVisibility:
    """Pure rule: role plus granted permissions -> what the caller may receive."""
    if role in FULL_ACCESS_ROLES:
        return ALL
    if role in GRANTABLE_ROLES:
        return FieldVisibility(
            supplier_names=SEE_SUPPLIER_NAMES in granted,
            cost_and_margin=SEE_COST_AND_MARGIN in granted,
        )
    return NONE  # agency and customer roles: never, even if a grant row exists


def granted_permissions(connection, user_id: str, role_id: int) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT permission FROM role_permissions WHERE role_id=:role "
            "UNION SELECT permission FROM user_permissions WHERE user_id=:user"
        ),
        {"role": role_id, "user": user_id},
    )
    return {row[0] for row in rows}


@dataclass(frozen=True)
class BackOfficeUser:
    user_id: str
    role: str
    visibility: FieldVisibility


def _back_office_user(identity, allowed: set[str] = BACK_OFFICE_ROLES, denied: str = "Back-office access denied.") -> BackOfficeUser:
    from app.db.agencies import transaction
    from app.db.users import ensure_user

    with transaction() as connection:
        profile = ensure_user(connection, identity)
        if profile.role not in allowed:
            raise HTTPException(403, denied)
        granted = granted_permissions(connection, profile.id, profile.role_id)
    return BackOfficeUser(user_id=profile.id, role=profile.role, visibility=resolve_visibility(profile.role, granted))


async def require_back_office(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> BackOfficeUser:
    return await run_in_threadpool(_back_office_user, user)


async def require_search_viewer(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> BackOfficeUser:
    """Internal or agency role. Customers are refused. Visibility as resolve_visibility."""
    return await run_in_threadpool(_back_office_user, user, SEARCH_ROLES, "Search access denied.")
