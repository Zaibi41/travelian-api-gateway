"""Reconciliation with a database (migration 0031): PayFast, bank suggestions, supplier checks, mismatch list."""

from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from app.db import finance, public_bookings
from app.db import reconciliation as recon
from app.services.statement_csv import Line
from tests.database.test_booking_payments import account_for
from tests.database.test_finance_foundations import WHO, fresh, pay

NOW = datetime.now(timezone.utc)
TODAY = NOW.astimezone().date()


def line(no, ref, amount, fee=None, net=None, desc=None, on=None):
    return Line(no, ref, desc, on or TODAY, amount, fee, net, None)


def card_payment(connection, metadata, basket, txn, pnr):
    reference = fresh(connection, metadata, pnr=pnr)
    public_bookings.create_attempt(reference, basket, 48_100, "PKR", card_fee_minor=1_400)
    public_bookings.record_result(basket, paid=True, err_code="000", transaction_id=txn, amount_minor=48_100, message=None, source="notification", now=NOW)
    return reference


def test_payfast_settlement_matches_records_fee_and_lists_differences(database):
    connection, metadata, _ = database
    card_payment(connection, metadata, "B1", "TXN-1", "PF0001")
    card_payment(connection, metadata, "B2", "TXN-2", "PF0002")
    card_payment(connection, metadata, "B3", "TXN-3", "PF0003")  # not in the file
    result = recon.import_payfast("settlement.csv", b"file-1", {"reference": "Txn"}, [
        line(2, "TXN-1", 48_100, fee=1_250, net=46_850), line(3, "txn-2", 48_000, fee=1_250, net=46_000), line(4, "TXN-9", 5_000)], WHO)
    assert (result.matched, result.unmatched, result.fees_recorded) == (2, 1, 2)
    kinds = sorted(m["kind"] for m in recon.list_mismatches("open")[0])
    assert kinds == ["payfast_amount_differs", "payfast_line_not_found", "payfast_net_differs", "payment_not_in_payfast"]
    fees = connection.execute(text("SELECT source, amount_minor FROM payment_fees")).all()
    assert sorted(fees) == [("settlement", 1_250), ("settlement", 1_250)]
    with pytest.raises(recon.ReconRefused, match="already imported"):
        recon.import_payfast("again.csv", b"file-1", {"reference": "Txn"}, [line(2, "TXN-1", 48_100)], WHO)


def test_bank_statement_suggests_staff_confirm_or_reject(database):
    connection, metadata, _ = database
    first = fresh(connection, metadata, pnr="BK0001")
    pay(first, 20_000, "IBFT-1")
    second = fresh(connection, metadata, pnr="BK0002")
    pay(second, 20_000, "IBFT-2")
    account = account_for("bank_transfer")
    result = recon.import_bank(account, "meezan.csv", b"bank-1", {"credit": "Credit"}, [
        line(2, None, 20_000, desc=f"IBFT from customer {second}", on=date(2026, 11, 10)),
        line(3, None, 99_900, desc="unknown", on=date(2026, 11, 9))], WHO)
    assert (result.suggested, result.unmatched) == (1, 1)
    lines = connection.execute(text("SELECT l.id, b.reference FROM recon_lines l JOIN bookings b ON b.id = l.booking_id WHERE l.status = 'suggested'")).all()
    assert [ref for _, ref in lines] == [second]  # the reference in the text decides between equal amounts
    recon.decide_suggestion(lines[0][0], WHO, True)
    with pytest.raises(recon.ReconRefused):
        recon.decide_suggestion(lines[0][0], WHO, False)
    kinds = sorted(m["kind"] for m in recon.list_mismatches("open")[0])
    assert kinds == ["bank_credit_unmatched", "payment_not_in_bank"]
    with pytest.raises(sa.exc.DBAPIError):  # a matched line is final, even bypassing the code
        with connection.begin_nested():
            connection.execute(text("UPDATE recon_lines SET status = 'unmatched' WHERE id = :id"), {"id": lines[0][0]})


def test_supplier_check_and_mismatch_resolution(database):
    connection, metadata, _ = database
    issued = fresh(connection, metadata, pnr="SP0001")
    connection.execute(text("UPDATE bookings SET booking_status = 'ticket_issued' WHERE reference = :r"), {"r": issued})
    other = fresh(connection, metadata, pnr="SP0002")
    connection.execute(text("UPDATE bookings SET booking_status = 'ticket_issued' WHERE reference = :r"), {"r": other})
    finance.add_supplier_charge(other, WHO, amount_minor=40_000, reason="Portal")
    assert recon.check_supplier_charges(WHO) == 2
    assert recon.check_supplier_charges(WHO) == 0  # listed once
    mismatches, total = recon.list_mismatches("open")
    assert total == 2 and {m["kind"] for m in mismatches} == {"ticket_without_charge", "charge_differs_from_cost"}
    target = mismatches[0]
    with pytest.raises(recon.ReconRefused, match="cost and margin"):
        recon.resolve_mismatch(target["id"], WHO, "resolved", "x", see_cost=False)
    with pytest.raises(recon.ReconRefused, match="plain words"):
        recon.resolve_mismatch(target["id"], WHO, "resolved", " ")
    done = recon.resolve_mismatch(target["id"], WHO, "explained", "Issued in the portal; charge to be added")
    assert done["status"] == "explained" and done["explained_by_user_id"] == WHO.user_id
    final = recon.resolve_mismatch(target["id"], WHO, "resolved", "Charge added")
    assert final["explanation"] == "Issued in the portal; charge to be added" and final["resolution_reason"] == "Charge added"
    with pytest.raises(recon.ReconRefused, match="already resolved"):
        recon.resolve_mismatch(target["id"], WHO, "explained", "again")
    with pytest.raises(sa.exc.DBAPIError):
        with connection.begin_nested():
            connection.execute(text("UPDATE recon_mismatches SET status = 'open' WHERE id = :id"), {"id": target["id"]})
    actions = [e.action for e in finance.list_audit(entity_type="recon_mismatch")[0]]
    assert actions == ["mismatch_resolved", "mismatch_explained"]


def test_supplier_statement_import(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata, pnr="SF7777", provider="sooperfare")
    finance.add_supplier_charge(reference, WHO, amount_minor=45_770, reason="Issued")
    result = recon.import_supplier("sooperfare", "sf.csv", b"sf-1", {"reference": "PNR"}, [line(2, "sf7777", 45_790), line(3, "NOPE", 100)], WHO)
    assert (result.matched, result.unmatched) == (1, 1)
    assert sorted(m["kind"] for m in recon.list_mismatches("open")[0]) == ["supplier_line_not_found", "supplier_statement_differs"]
    assert date.today() is not None
