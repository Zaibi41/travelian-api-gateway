"""Issue 224 against PostgreSQL (migration 0038): B2B portal and Hotels supplier types; B2B agency wallets (D3)."""

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db import agency_wallets as wallets, finance, payment_accounts, supplier_ledger as ledger
from tests.database.test_booking_people import ACTOR

FIN = finance.Actor(ACTOR, "super_admin")
NOW = datetime.now(timezone.utc) - timedelta(minutes=1)
RS = 100


@pytest.fixture
def agency(database):
    connection, metadata, _ = database
    if not connection.execute(sa.text("SELECT 1 FROM users WHERE id=:id"), {"id": ACTOR}).first():
        connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    connection.execute(sa.text("INSERT INTO agencies (id, name, status) VALUES ('karachi-travels', 'Karachi Travels', 'active')"))
    return "karachi-travels"


def refused_code(fn, *args, **kwargs) -> str:
    with pytest.raises(finance.FinanceRefused) as error:
        fn(*args, **kwargs)
    return error.value.code


def test_b2b_portal_and_hotels_supplier_types(database):
    sections = {b.supplier.id: b.supplier.section for b in ledger.balances()}
    assert sections["aeroglobe"] == "b2b" and sections["ratehawk"] == "hotels"
    hotels = next(b.supplier for b in ledger.balances() if b.supplier.id == "ratehawk")
    assert (hotels.booking_mode, hotels.balance_mode) == ("api", "manual")
    order = [b.supplier.section for b in ledger.balances()]
    assert order == sorted(order, key=["api", "direct", "b2b", "hotels"].index)  # API, direct IDs, B2B portals, hotels


def test_add_a_group_ticketing_portal_as_b2b(agency):
    s = ledger.add_supplier(FIN, name="Group Ticketing Portal", section="b2b", settlement_tag=None, low_threshold_minor=None)
    assert s.section == "b2b"
    assert refused_code(ledger.add_supplier, FIN, name="Something", section="nope", settlement_tag=None, low_threshold_minor=None) == "invalid_section"


def test_agency_wallet_deposit_adjustment_statement_and_never_below_zero(agency):
    account = payment_accounts.add("bank_transfer", "UBL Business").id
    assert next(w for w in wallets.wallets() if w.agency_id == agency).balance_minor == 0
    assert refused_code(wallets.add_entry, FIN, agency, kind="deposit", amount_minor=50_000 * RS, reference=None, reason=None,
                        occurred_at=NOW, account_id=account) == "reference_required"
    wallets.add_entry(FIN, agency, kind="deposit", amount_minor=50_000 * RS, reference="UBL-77", reason=None, occurred_at=NOW, account_id=account)
    after = wallets.add_entry(FIN, agency, kind="adjustment", amount_minor=-5_000 * RS, reference=None, reason="Fee agreed", occurred_at=NOW,
                              account_id=None)
    assert after.balance_minor == 45_000 * RS and after.deposits_minor == 50_000 * RS
    assert refused_code(wallets.add_entry, FIN, agency, kind="adjustment", amount_minor=-50_000 * RS, reference=None, reason="x",
                        occurred_at=NOW, account_id=None) == "negative_balance"
    w, entries = wallets.statement(agency)
    assert [(e.kind, e.amount_minor, e.balance_minor) for e in entries] == [("deposit", 50_000 * RS, 50_000 * RS),
                                                                           ("adjustment", -5_000 * RS, 45_000 * RS)]
    assert entries[0].account == "UBL Business" and entries[0].by_role  # who recorded it (their role)
    low = wallets.set_low(FIN, agency, 60_000 * RS)
    assert low.low is True
    with pytest.raises(Exception):  # append-only
        with wallets.agencies.application_engine().begin() as c:
            c.execute(sa.text("DELETE FROM agency_wallet_entries"))
