"""D23: supplier names, cost and margin are decided by the gateway, per role."""

import pytest
from fastapi import HTTPException

from app.core import field_visibility as fv
from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, resolve_visibility
from tests.core.test_admin_dashboard import RECORDS, booking, client

BOTH = {fv.SEE_SUPPLIER_NAMES, fv.SEE_COST_AND_MARGIN}


@pytest.mark.parametrize("role, granted, expected", [
    ("super_admin", set(), ALL),
    ("staff", set(), NONE),
    ("staff", {fv.SEE_SUPPLIER_NAMES}, FieldVisibility(supplier_names=True, cost_and_margin=False)),
    ("staff", {fv.SEE_COST_AND_MARGIN}, FieldVisibility(supplier_names=False, cost_and_margin=True)),
    ("staff", BOTH, ALL),
    # Agency and customer roles: never, even when a grant row exists.
    ("agency_owner", BOTH, NONE),
    ("customer", BOTH, NONE),
    ("unknown_role", BOTH, NONE),
])
def test_role_matrix(role, granted, expected):
    assert resolve_visibility(role, granted) == expected


def staff(granted=frozenset()):
    return BackOfficeUser(user_id="u-staff", role="staff", visibility=resolve_visibility("staff", set(granted)))


ROWS = [booking(1), booking(2, total_minor=500_000, supplier_cost_minor=450_000)]


def test_dashboard_omits_margin_and_supplier_names_for_plain_staff(monkeypatch):
    api = client(monkeypatch, ROWS, viewer=staff())
    summary = api.get("/v1/admin/dashboard/summary").json()
    assert "gross_profit" not in summary["business"]          # margin: absent, not null
    assert summary["business"]["total_sales"]["amount_minor"] == 1_500_000
    options = api.get("/v1/admin/dashboard/filter-options").json()
    assert "providers" not in options                          # supplier names: absent
    assert [o["value"] for o in options["products"]] == ["flight", "hotel", "umrah"]
    assert "SooperFare" not in api.get("/v1/admin/dashboard/filter-options").text
    for path in ("/v1/admin/dashboard/summary", "/v1/admin/attention-queue"):
        assert api.get(path, params={"provider": "sooperfare"}).status_code == 403


def test_super_admin_and_granted_staff_see_the_fields(monkeypatch):
    for viewer in (BackOfficeUser(user_id="a", role="super_admin", visibility=ALL), staff(BOTH)):
        api = client(monkeypatch, ROWS, viewer=viewer)
        assert api.get("/v1/admin/dashboard/summary").json()["business"]["gross_profit"]["amount_minor"] == 150_000
        assert [o["label"] for o in api.get("/v1/admin/dashboard/filter-options").json()["providers"]] == ["SooperFare"]
        assert api.get("/v1/admin/dashboard/summary", params={"provider": "sooperfare"}).status_code == 200


def test_grants_are_independent(monkeypatch):
    names_only = client(monkeypatch, ROWS, viewer=staff({fv.SEE_SUPPLIER_NAMES}))
    assert "providers" in names_only.get("/v1/admin/dashboard/filter-options").json()
    assert "gross_profit" not in names_only.get("/v1/admin/dashboard/summary").json()["business"]
    cost_only = client(monkeypatch, ROWS, viewer=staff({fv.SEE_COST_AND_MARGIN}))
    assert "providers" not in cost_only.get("/v1/admin/dashboard/filter-options").json()
    assert "gross_profit" in cost_only.get("/v1/admin/dashboard/summary").json()["business"]


@pytest.mark.parametrize("role", ["agency_owner", "customer"])
def test_agency_and_customer_are_refused_before_any_data(monkeypatch, role):
    class Profile:
        id, role_id, active = "u-x", 2, True
    Profile.role = role

    class Tx:
        def __enter__(self): return object()
        def __exit__(self, *exc): return False

    import app.db.agencies as agencies
    import app.db.users as users
    monkeypatch.setattr(agencies, "transaction", lambda: Tx())
    monkeypatch.setattr(users, "ensure_user", lambda connection, identity: Profile)
    monkeypatch.setattr(fv, "granted_permissions", lambda *a: pytest.fail("grants must not be read for non-back-office roles"))
    with pytest.raises(HTTPException) as error:
        fv._back_office_user(object())
    assert error.value.status_code == 403
