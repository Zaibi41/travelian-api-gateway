"""Ledger (travelian#72) without a database: who may use it, and journal line rules."""

import pytest

import app.core.field_visibility as fv
from app.db import ledger
from app.main import app
from tests.core.test_finance import _client


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


@pytest.mark.parametrize("role,granted", [("staff", {fv.FINANCE}), ("staff", {fv.SEE_COST_AND_MARGIN}), ("agency_owner", {fv.FINANCE, fv.SEE_COST_AND_MARGIN})])
def test_ledger_needs_finance_and_cost(monkeypatch, role, granted):
    client = _client(monkeypatch, role, granted)
    for path in ("/v1/admin/finance/ledger/accounts", "/v1/admin/finance/ledger/trial-balance", "/v1/admin/finance/ledger/export.xlsx"):
        assert client.get(path).status_code == 403


def test_manual_lines_are_one_sided(monkeypatch):
    client = _client(monkeypatch, "super_admin")
    body = {"posted_on": "2026-11-21", "description": "x", "lines": [{"account_code": "1100", "debit_minor": 5, "credit_minor": 5},
                                                                      {"account_code": "3000", "credit_minor": 0}]}
    assert client.post("/v1/admin/finance/ledger/journals", json=body).status_code == 422


def test_signed_amounts_reverse_sides():
    assert ledger._signed("5000", "1320", 100) == [("5000", 100, 0, None), ("1320", 0, 100, None)]
    assert ledger._signed("5000", "1320", -100) == [("1320", 100, 0, None), ("5000", 0, 100, None)]
