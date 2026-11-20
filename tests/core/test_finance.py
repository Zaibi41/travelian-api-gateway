"""Finance (travelian#91) without a database: profit maths in integer paisa, and who may call the Finance API."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.core.field_visibility as fv
from app.core.authentication import require_authenticated_user
from app.main import app
from app.services.finance_profit import ProfitInputs, calculate


def test_profit_formula_selling_plus_fees_minus_charge_fees_refunds():
    p = calculate(ProfitInputs(selling_minor=100_000, stored_cost_minor=90_000, cost_known=True, fees_charged_minor=3_000,
                               supplier_charges_minor=[45_000, 44_500], payment_fees_minor=[2_900, -100], refunds_paid_minor=[1_000]))
    # 100,000 + 3,000 - 89,500 - 2,800 - 1,000
    assert p.profit_minor == 9_700 and p.supplier_charge_basis == "recorded" and p.missing == ()
    assert all(isinstance(v, int) for v in (p.profit_minor, p.payment_fees_minor, p.refunds_minor, p.supplier_charge_minor))


def test_profit_uses_the_quoted_cost_until_a_charge_is_recorded():
    p = calculate(ProfitInputs(selling_minor=46_700, stored_cost_minor=45_770, cost_known=True))
    assert (p.profit_minor, p.supplier_charge_basis) == (930, "quoted")


def test_profit_not_known_when_cost_is_not_known_never_zero():
    p = calculate(ProfitInputs(selling_minor=46_700, stored_cost_minor=46_700, cost_known=False))
    assert p.profit_minor is None and p.supplier_charge_basis == "not_known"
    # A recorded charge makes it known again.
    p = calculate(ProfitInputs(selling_minor=46_700, stored_cost_minor=46_700, cost_known=False, supplier_charges_minor=[45_000]))
    assert p.profit_minor == 1_700


def test_card_payment_without_a_fee_is_reported_missing():
    p = calculate(ProfitInputs(selling_minor=10_000, stored_cost_minor=9_000, cost_known=True, card_payments_without_fee=1))
    assert p.missing == ("payment_fees",) and p.profit_minor == 1_000


def test_profit_refuses_floats():
    with pytest.raises(TypeError):
        calculate(ProfitInputs(selling_minor=100.5, stored_cost_minor=1, cost_known=True))


def _client(monkeypatch, role, granted=frozenset()):
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
    monkeypatch.setattr(fv, "granted_permissions", lambda *a: set(granted))
    app.dependency_overrides[require_authenticated_user] = lambda: object()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


@pytest.mark.parametrize("role,granted", [("staff", set()), ("staff", {fv.RECORD_PAYMENTS, fv.SEE_COST_AND_MARGIN}),
                                          ("agency_owner", {fv.FINANCE}), ("customer", {fv.FINANCE})])
@pytest.mark.parametrize("method,path", [("get", "/v1/admin/finance/audit-log"), ("get", "/v1/admin/finance/refunds"),
                                         ("get", "/v1/admin/finance/bookings/TRV-ABC123"),
                                         ("post", "/v1/admin/finance/refunds/r1/approve")])
def test_finance_routes_need_the_finance_permission(monkeypatch, role, granted, method, path):
    client = _client(monkeypatch, role, granted)
    response = getattr(client, method)(path)
    assert response.status_code == 403


def test_finance_access_reports_the_permission(monkeypatch):
    assert _client(monkeypatch, "staff").get("/v1/admin/finance/access").json() == {"finance": False}
    assert _client(monkeypatch, "staff", {fv.FINANCE}).get("/v1/admin/finance/access").json() == {"finance": True}
    assert _client(monkeypatch, "super_admin").get("/v1/admin/finance/access").json() == {"finance": True}


def test_staff_granted_finance_passes_the_permission_check(monkeypatch):
    # Past the permission check the database is reached (unavailable in unit tests: 503), never 403.
    client = _client(monkeypatch, "staff", {fv.FINANCE})
    from app.db import finance

    monkeypatch.setattr(finance, "list_refunds", lambda *a: ([], 0))
    assert client.get("/v1/admin/finance/refunds").json() == {"refunds": [], "total": 0}


def test_require_finance_refuses_without_grant(monkeypatch):
    _client(monkeypatch, "staff")
    with pytest.raises(HTTPException) as error:
        fv._finance_user(object())
    assert error.value.status_code == 403
