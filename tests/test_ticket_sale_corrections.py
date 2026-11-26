"""Editing and deleting a Ticket Sale Entry is Super Admin only: the gateway decides, never the page."""

import pytest

from app.api.routes import finance_ops as routes
from app.core.field_visibility import FULL_ACCESS_ROLES, require_super_admin

PATHS = {("POST", "/admin/finance/ticket-sales/{sale_id}/correct"), ("POST", "/admin/finance/ticket-sales/{sale_id}/correct/preview"),
         ("POST", "/admin/finance/ticket-sales/{sale_id}/delete")}


def _dependencies(route) -> set:
    found, stack = set(), list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        found.add(d.call)
        stack.extend(d.dependencies)
    return found


@pytest.mark.parametrize("method,path", sorted(PATHS))
def test_edit_and_delete_routes_need_super_admin(method, path):
    route = next(r for r in routes.router.routes if r.path == path and method in r.methods)
    assert require_super_admin in _dependencies(route)


def test_super_admin_is_the_only_full_access_role():
    assert FULL_ACCESS_ROLES == {"super_admin"}


def test_correction_needs_a_reason():
    body = dict(channel="direct", passenger_name="A", cost_minor=0, sale_minor=0)
    with pytest.raises(ValueError):
        routes.CorrectionRequest(**body)
    assert routes.CorrectionRequest(**body, reason="Typo").to_input().passenger_name == "A"
