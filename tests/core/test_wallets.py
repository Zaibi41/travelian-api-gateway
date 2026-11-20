"""Supplier wallets (travelian#68) without a database: permissions, Southair read-only parsing, Issue panel warning."""

import asyncio
from datetime import datetime, timezone

import pytest

import app.core.field_visibility as fv
from app.db import supplier_wallets as store
from app.main import app
from app.services import supplier_wallets as service
from tests.core.test_finance import _client


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


@pytest.mark.parametrize("role,granted", [("staff", {fv.FINANCE}), ("staff", {fv.SEE_COST_AND_MARGIN}), ("agency_owner", {fv.FINANCE, fv.SEE_COST_AND_MARGIN})])
def test_balances_need_finance_and_cost_permission(monkeypatch, role, granted):
    client = _client(monkeypatch, role, granted)
    assert client.get("/v1/admin/finance/supplier-balances").status_code == 403
    assert client.post("/v1/admin/finance/supplier-balances/southair/refresh").status_code == 403


def test_finance_with_cost_sees_balances(monkeypatch):
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "wallets", lambda: [store.Wallet("southair", "Southair", "api", 12_345_600, "PKR", now, None, 20_000_000),
                                                   store.Wallet("sooperfare", "SooperFare", "calculated", None, "PKR", None, None, None)])
    body = _client(monkeypatch, "staff", {fv.FINANCE, fv.SEE_COST_AND_MARGIN}).get("/v1/admin/finance/supplier-balances").json()
    southair, sooperfare = body["wallets"]
    assert southair["balance"]["amount_minor"] == 12_345_600 and southair["low"] is True
    assert sooperfare["balance"] is None and "no balance API" in sooperfare["note"]


class FakeSouthair:
    def __init__(self, cap):
        self.cap = cap
        self.calls = []

    async def profile(self):
        self.calls.append("profile")
        return {"limit": {"cap": self.cap}}


def test_southair_balance_is_read_only_and_exact():
    fake = FakeSouthair("1,234,567.89")
    assert asyncio.run(service.read_southair(lambda: fake)) == (123_456_789, "PKR")
    assert fake.calls == ["profile"]  # nothing else is called
    with pytest.raises(service.BalanceUnavailable):
        asyncio.run(service.read_southair(lambda: FakeSouthair(None)))


def test_issue_warning(monkeypatch):
    def wallet(provider, connection=None):
        return store.Wallet(provider, "SooperFare", "calculated", 4_000_000, "PKR", None, None, 5_000_000)
    monkeypatch.setattr(store, "wallet", wallet)
    assert "less than this ticket's Rs 45,000" in service.issue_warning("sooperfare", 4_500_000, True)
    assert "is low" in service.issue_warning("sooperfare", 100_000, True)
    hidden = service.issue_warning("sooperfare", 4_500_000, False)
    assert "SooperFare" not in hidden and "Rs" not in hidden
    assert service.issue_warning("airblue", 4_500_000, True) is None


def test_low_balance_alert_and_audit_hide_supplier_figures(monkeypatch):
    from app.api.routes.finance import hides_cost
    from app.services import notifications

    recorded = []
    monkeypatch.setattr(notifications, "_record", lambda kind, reference, title, details: recorded.append((title, details)) or "n1")
    monkeypatch.setattr(notifications, "_send_email", lambda subject, body, html=None: recorded.append((subject, body)) or False)
    asyncio.run(service._low_balance_alert(store.Wallet("sooperfare", "SooperFare", "calculated", 4_000_000, "PKR", None, None, 5_000_000)))
    assert "sooperfare" not in str(recorded).lower() and "40,000" not in str(recorded) and "Rs" not in str(recorded)
    assert hides_cost("supplier_wallet", "sooperfare") and hides_cost("finance_setting", "low_balance_southair_minor")
    assert not hides_cost("finance_setting", "card_fee_bps")
