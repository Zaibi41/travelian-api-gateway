"""Ledger with a database (migration 0032, travelian#72): automatic journals, balance, maker/checker, reports, gaps."""

from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from app.api.routes.finance import build_pnl, build_statement, build_trial_balance
from app.db import finance, ledger, public_bookings
from app.db import supplier_wallets as wallets
from tests.database.test_finance_foundations import ACTOR, WHO, fresh, pay

NOW = datetime.now(timezone.utc)
CHECKER = finance.Actor("22222222-2222-4222-8222-222222222222", "super_admin")


def checker(connection, metadata):
    if not connection.execute(text("SELECT 1 FROM users WHERE id = :id"), {"id": CHECKER.user_id}).first():
        connection.execute(metadata.tables["users"].insert().values(id=CHECKER.user_id, cognito_issuer="https://t.invalid", cognito_sub="checker"))


def test_events_post_balanced_journals_once_and_reports_add_up(database):
    connection, metadata, _ = database
    card = fresh(connection, metadata, pnr="LG0001")
    public_bookings.create_attempt(card, "LB1", 48_100, "PKR", card_fee_minor=1_400)
    public_bookings.record_result("LB1", paid=True, err_code="000", transaction_id="LT1", amount_minor=48_100, message=None, source="notification", now=NOW)
    payment_id = connection.scalar(text("SELECT id FROM booking_payments WHERE reference = 'LT1'"))
    finance.add_payment_fee(payment_id, WHO, amount_minor=1_250, reason="PayFast statement")
    connection.execute(text("UPDATE bookings SET booking_status = 'ticket_issued' WHERE reference = :r"), {"r": card})
    finance.add_supplier_charge(card, WHO, amount_minor=45_770, reason="Issued")
    bank = fresh(connection, metadata, pnr="LG0002")
    pay(bank, 46_700, "IBFT-LG")
    refund = finance.request_refund(bank, WHO, amount_minor=46_700, method="bank_transfer", reason="Cancelled")
    finance.approve_refund(refund.id, WHO)
    finance.mark_refund_paid(refund.id, WHO, payout_reference="IBFT-OUT", paid_at=NOW)
    wallets.add_entry("sooperfare", WHO, kind="top_up", amount_minor=500_000, reference="TOP-1", reason=None, occurred_at=NOW)
    connection.execute(text("UPDATE bookings SET is_test = true WHERE reference = :r"), {"r": fresh(connection, metadata, pnr="TEST01")})

    first = ledger.post_pending(WHO)
    assert first.journals == 7  # card payment, bank payment, ticket, supplier charge, fee, refund, top-up
    assert ledger.post_pending(WHO).journals == 0  # idempotent backfill
    tb = build_trial_balance(None)
    assert tb.balanced and tb.total_debit.amount_minor == tb.total_credit.amount_minor
    by_code = {a.code: a.balance.amount_minor for a in tb.accounts}
    assert by_code["4000"] == 46_700 and by_code["4100"] == 1_400 and by_code["5000"] == 45_770 and by_code["5100"] == 1_250
    assert by_code["2100"] == 0          # card booking ticketed (deposit used), bank booking refunded before ticketing
    assert by_code["1150"] == 48_100 - 1_250
    pnl = build_pnl(None, None)
    assert pnl.net_profit.amount_minor == 46_700 + 1_400 - 45_770 - 1_250 == 1_080
    statement = build_statement("1150", None, None)
    assert statement.closing.amount_minor == 46_850 and len(statement.entries) == 2
    assert {g["reason"] for g in ledger.gaps()} == set()  # test booking had no money, so nothing to flag
    with pytest.raises(sa.exc.DBAPIError):  # posted journal lines never change
        with connection.begin_nested():
            connection.execute(text("UPDATE journal_lines SET debit_minor = debit_minor + 1"))


def test_ticket_without_known_cost_is_a_gap(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata, pnr="GAP001", supplier_total_minor=46_700)
    connection.execute(text("UPDATE bookings SET booking_status = 'ticket_issued', cost_known = false WHERE reference = :r"), {"r": reference})
    ledger.post_pending(WHO)
    assert [(g["reason"], g["booking_reference"]) for g in ledger.gaps()] == [("cost_not_known", reference)]


def test_manual_journal_maker_checker_and_balance(database):
    connection, metadata, _ = database
    fresh(connection, metadata)
    checker(connection, metadata)
    with pytest.raises(ledger.LedgerRefused, match="equal"):
        ledger.create_manual(WHO, date(2026, 11, 21), "Opening balance", [("1100", 100_000, 0, None), ("3000", 0, 90_000, None)])
    journal_id = ledger.create_manual(WHO, date(2026, 11, 21), "Opening balance", [("1100", 100_000, 0, None), ("3000", 0, 100_000, None)])
    assert build_trial_balance(None).accounts == []  # pending: not in the books yet
    with pytest.raises(ledger.LedgerRefused, match="maker/checker"):
        ledger.decide_manual(journal_id, WHO, True)
    ledger.decide_manual(journal_id, CHECKER, True)
    tb = build_trial_balance(None)
    assert tb.balanced and {a.code: a.balance.amount_minor for a in tb.accounts} == {"1100": 100_000, "3000": 100_000}
    with pytest.raises(ledger.LedgerRefused, match="already posted"):
        ledger.decide_manual(journal_id, CHECKER, False, "late")
    with pytest.raises(sa.exc.DBAPIError):  # an unbalanced journal is refused by the database itself
        with connection.begin_nested():
            connection.execute(text("INSERT INTO journals (id, posted_on, description, source_type, status, created_by_user_id)"
                                    " VALUES ('j-x', '2026-11-21', 'x', 'manual', 'pending_approval', :u)"), {"u": ACTOR})
            connection.execute(text("INSERT INTO journal_lines (journal_id, account_code, debit_minor) VALUES ('j-x', '1100', 5)"))
            connection.execute(text("SET CONSTRAINTS journal_lines_balanced IMMEDIATE"))
    actions = [e.action for e in finance.list_audit(entity_type="journal")[0]]
    assert actions == ["manual_journal_approved", "manual_journal_created"]


def test_chart_of_accounts_edits(database):
    connection, metadata, _ = database
    fresh(connection, metadata)
    assert ledger.add_account("6100", "Office rent", "expense", WHO)["name"] == "Office rent"
    with pytest.raises(ledger.LedgerRefused, match="exists"):
        ledger.add_account("6100", "Again", "expense", WHO)
    with pytest.raises(ledger.LedgerRefused, match="stays active"):
        ledger.update_account("4000", WHO, active=False)
    assert ledger.update_account("4000", WHO, name="Ticket sales (flights and groups)")["name"].startswith("Ticket sales")
    assert ledger.update_account("6100", WHO, active=False)["active"] is False
