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
#: Action permissions (what a user may do, not see): super_admin always; staff when granted.
RECORD_PAYMENTS = "record_payments"
ISSUE_TICKETS = "issue_tickets"
MANAGE_PEOPLE = "manage_people"
MERGE_PEOPLE = "merge_people"
ACTIONS = (RECORD_PAYMENTS, ISSUE_TICKETS, MANAGE_PEOPLE, MERGE_PEOPLE)
PERMISSIONS = (SEE_SUPPLIER_NAMES, SEE_COST_AND_MARGIN, *ACTIONS)  # reveal_identity is gone: nothing is masked

FULL_ACCESS_ROLES = {"super_admin"}
GRANTABLE_ROLES = {"staff"}
BACK_OFFICE_ROLES = FULL_ACCESS_ROLES | GRANTABLE_ROLES
#: Finance > Accounts & Ledgers (receiving accounts: list all, add, deactivate). The spec gives this
#: to Finance and Super Admin; there is no Finance role yet, so Super Admin only. Add "finance" here
#: when that role exists. Staff, agency and customer roles get 403.
FINANCE_ACCOUNT_ROLES = {"super_admin"}
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


def resolve_actions(role: str, granted: set[str] | frozenset[str]) -> frozenset[str]:
    """Pure rule for action permissions: super_admin all, staff what is granted, others none."""
    if role in FULL_ACCESS_ROLES:
        return frozenset(ACTIONS)
    if role in GRANTABLE_ROLES:
        return frozenset(ACTIONS) & frozenset(granted)
    return frozenset()


@dataclass(frozen=True)
class BackOfficeUser:
    user_id: str
    role: str
    visibility: FieldVisibility
    actions: frozenset[str] = frozenset()

    def can(self, action: str) -> bool:
        return action in self.actions


def _back_office_user(identity, allowed: set[str] = BACK_OFFICE_ROLES, denied: str = "Back-office access denied.") -> BackOfficeUser:
    from app.db.agencies import transaction
    from app.db.users import ensure_user

    with transaction() as connection:
        profile = ensure_user(connection, identity)
        if profile.role not in allowed:
            raise HTTPException(403, denied)
        granted = granted_permissions(connection, profile.id, profile.role_id)
    return BackOfficeUser(user_id=profile.id, role=profile.role, visibility=resolve_visibility(profile.role, granted),
                          actions=resolve_actions(profile.role, granted))


async def require_back_office(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> BackOfficeUser:
    return await run_in_threadpool(_back_office_user, user)


async def require_finance_accounts(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> BackOfficeUser:
    """Finance > Accounts & Ledgers: FINANCE_ACCOUNT_ROLES only, everyone else 403."""
    return await run_in_threadpool(_back_office_user, user, FINANCE_ACCOUNT_ROLES, "Accounts & Ledgers access denied.")


async def require_search_viewer(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> BackOfficeUser:
    """Internal or agency role. Customers are refused. Visibility as resolve_visibility."""
    return await run_in_threadpool(_back_office_user, user, SEARCH_ROLES, "Search access denied.")
