"""Supplier wallets with a database (migration 0030): calculated balance, top-ups, adjustments, alert once, history."""

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from app.db import finance
from app.db import supplier_wallets as store
from tests.database.test_finance_foundations import WHO, fresh

T0 = datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc)


def test_sooperfare_calculated_balance_counts_charges_since_tracking(database):
    connection, metadata, _ = database
    old = fresh(connection, metadata, provider="sooperfare", pnr="SFOLD1")
    finance.add_supplier_charge(old, WHO, amount_minor=10_000, reason="before tracking")  # before the first top-up: not counted
    assert store.wallet("sooperfare").balance_minor is None
    store.add_entry("sooperfare", WHO, kind="top_up", amount_minor=500_000, reference="IBFT-1", reason=None, occurred_at=T0)
    reference = fresh(connection, metadata, provider="sooperfare", pnr="SFNEW1")
    finance.add_supplier_charge(reference, WHO, amount_minor=45_770, reason="Issued in portal")
    store.add_entry("sooperfare", WHO, kind="adjustment", amount_minor=-1_230, reason="Portal shows Rs 4,530", reference=None, occurred_at=T0)
    w = store.wallet("sooperfare")
    assert (w.balance_minor, w.mode) == (500_000 - 45_770 - 1_230, "calculated") and w.tracking_since is not None
    kinds = [h.kind for h in store.history("sooperfare")]
    assert {"top_up", "adjustment", "ticket"} <= set(kinds)
    actions = [e.action for e in finance.list_audit(entity_type="supplier_wallet")[0]]
    assert actions == ["supplier_adjustment_recorded", "supplier_top_up_recorded"]


def test_entry_rules_and_append_only(database):
    connection, metadata, _ = database
    fresh(connection, metadata)
    with pytest.raises(finance.FinanceRefused, match="reference"):
        store.add_entry("sooperfare", WHO, kind="top_up", amount_minor=100, reference=" ", reason=None, occurred_at=T0)
    with pytest.raises(finance.FinanceRefused, match="why"):
        store.add_entry("sooperfare", WHO, kind="adjustment", amount_minor=100, reference=None, reason="", occurred_at=T0)
    with pytest.raises(finance.FinanceRefused, match="own system"):
        store.add_entry("southair", WHO, kind="top_up", amount_minor=100, reference="x", reason=None, occurred_at=T0)
    store.add_entry("sooperfare", WHO, kind="top_up", amount_minor=100, reference="x", reason=None, occurred_at=T0)
    store.record_snapshot("southair", 9_900, "PKR", WHO)
    for table in ("supplier_wallet_entries", "supplier_balance_snapshots"):
        with pytest.raises(sa.exc.DBAPIError, match="never changed or deleted"):
            with connection.begin_nested():
                connection.execute(text(f"DELETE FROM {table}"))


def test_southair_snapshot_and_alert_fires_once_per_drop(database):
    connection, metadata, _ = database
    fresh(connection, metadata)
    finance.set_setting(store.threshold_key("southair"), 1_000_000, WHO)
    store.record_snapshot("southair", 900_000, "PKR", finance.SYSTEM)
    w = store.wallet("southair")
    assert (w.balance_minor, w.low) == (900_000, True)
    assert store.alert_state("southair", True, T0) is True
    assert store.alert_state("southair", True, T0 + timedelta(minutes=30)) is False   # still low: no repeat
    assert store.alert_state("southair", False, T0 + timedelta(hours=1)) is False      # back up: reset
    assert store.alert_state("southair", True, T0 + timedelta(hours=2)) is True        # a new drop alerts again
