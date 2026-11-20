"""Phase 1 payments with a database: slips (submit, confirm, reject, guards), PayFast with a card fee, Finance > Payments
list, daily totals, search, refunds due, settings audit."""

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from app.db import finance, finance_payments, payment_slips, public_bookings
from tests.database.test_booking_payments import account_for
from tests.database.test_finance_foundations import WHO, booking_id, fresh, pay

NOW = datetime(2026, 11, 21, 7, 0, tzinfo=timezone.utc)


def slip(reference, amount=20_000, key="payment-proof/11111111-1111-4111-8111-111111111111.pdf", account=None):
    return payment_slips.submit(reference, method="bank_transfer", account_id=account or account_for("bank_transfer"), amount_minor=amount,
                                payer_reference="IBFT-77", asset_key=key, content_type="application/pdf", size_bytes=1200, via="checkout")


def test_slip_confirm_records_the_payment_and_links_it(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)
    s = slip(reference)
    assert s.status == "awaiting_check" and s.account_label
    assert connection.scalar(text("SELECT chosen_account_id FROM bookings WHERE reference=:r"), {"r": reference}) == s.account_id
    assert connection.scalar(text("SELECT count(*) FROM booking_payments")) == 0  # a slip is not money yet
    done = payment_slips.confirm(s.id, WHO, amount_minor=19_000, account_id=s.account_id, payer_reference=None, received_at=NOW)
    assert done.status == "confirmed" and done.payment_id and done.reviewed_by_user_id == WHO.user_id
    row = connection.execute(text("SELECT amount_minor, reference FROM booking_payments WHERE id=:p"), {"p": done.payment_id}).one()
    assert tuple(row) == (19_000, "IBFT-77")
    assert connection.scalar(text("SELECT payment_status FROM bookings WHERE reference=:r"), {"r": reference}) == "part_paid"
    with pytest.raises(payment_slips.SlipRefused, match="already confirmed"):
        payment_slips.reject(s.id, WHO, "late")
    assert [e.action for e in finance.list_audit(booking_reference=reference)[0]] == ["payment_slip_confirmed"]


def test_slip_confirm_failure_records_nothing(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)
    s = slip(reference)
    with pytest.raises(payment_slips.SlipRefused, match="more than the amount due"):
        payment_slips.confirm(s.id, WHO, amount_minor=99_999_999, account_id=s.account_id, payer_reference=None, received_at=NOW)
    assert connection.scalar(text("SELECT status FROM payment_slips WHERE id=:id"), {"id": s.id}) == "awaiting_check"
    assert connection.scalar(text("SELECT count(*) FROM booking_payments")) == 0


def test_slip_rules_and_guards(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)
    with pytest.raises(payment_slips.SlipRefused, match="more than the amount due"):
        slip(reference, amount=46_701)
    s = slip(reference)
    with pytest.raises(payment_slips.SlipRefused, match="why"):
        payment_slips.reject(s.id, WHO, " ")
    rejected = payment_slips.reject(s.id, WHO, "No money arrived in HBL")
    assert rejected.status == "rejected" and rejected.rejection_reason == "No money arrived in HBL"
    for sql in ("UPDATE payment_slips SET amount_minor = 1", "UPDATE payment_slips SET status = 'awaiting_check'", "DELETE FROM payment_slips"):
        with pytest.raises(sa.exc.DBAPIError):
            with connection.begin_nested():
                connection.execute(text(sql))
    with pytest.raises(sa.exc.DBAPIError):  # never a public folder
        with connection.begin_nested():
            slip(reference, key="site/logo.png")


def test_payfast_card_fee_is_kept_apart_from_the_fare(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)  # Rs 467 total
    public_bookings.create_attempt(reference, "B-FEE-1", 48_100, "PKR", card_fee_minor=1_400)
    outcome = public_bookings.record_result("B-FEE-1", paid=True, err_code="000", transaction_id="T9", amount_minor=48_100, message=None,
                                            source="notification", now=NOW)
    assert (outcome.status, outcome.payment_status) == ("paid", "paid")
    row = connection.execute(text("SELECT amount_minor, card_fee_minor FROM booking_payments WHERE booking_id=:b"),
                             {"b": booking_id(connection, reference)}).one()
    assert tuple(row) == (46_700, 1_400)
    # The fee is not refunded: only the fare money can be.
    assert finance.refundable_minor(connection, booking_id(connection, reference)) == 46_700
    # A PayFast answer for the fare alone (without the fee) is a mismatch, never a payment.
    other = fresh(connection, metadata, pnr="FEE002")
    public_bookings.create_attempt(other, "B-FEE-2", 48_100, "PKR", card_fee_minor=1_400)
    assert public_bookings.record_result("B-FEE-2", paid=True, err_code="000", transaction_id="T10", amount_minor=46_700, message=None,
                                         source="return", now=NOW).status == "amount_mismatch"


def test_payments_list_filters_totals_and_search(database):
    connection, metadata, _ = database
    first = fresh(connection, metadata, pnr="LIST01")
    second = fresh(connection, metadata, pnr="LIST02")
    pay(first, 10_000, "TXN-A")
    pay(second, 5_000, "JC-1", method="jazzcash")
    s = slip(second, amount=2_000, key="payment-proof/22222222-2222-4222-8222-222222222222.png")
    page = finance_payments.list_payments(finance_payments.PaymentQuery())
    assert page.total == 3 and page.confirmed_minor == 15_000 and (page.awaiting_count, page.awaiting_minor) == (1, 2_000)
    assert {(d.method, d.amount_minor) for d in page.days} == {("bank_transfer", 10_000), ("jazzcash", 5_000)}
    assert [r["kind"] for r in finance_payments.list_payments(finance_payments.PaymentQuery(status="awaiting_check")).rows] == ["slip"]
    assert finance_payments.list_payments(finance_payments.PaymentQuery(method="jazzcash")).total == 1
    assert finance_payments.list_payments(finance_payments.PaymentQuery(q="list01")).total == 1
    assert finance_payments.list_payments(finance_payments.PaymentQuery(q="txn-a")).total == 1
    assert finance_payments.list_payments(finance_payments.PaymentQuery(account_id=s.account_id)).total == 2
    later = finance_payments.PaymentQuery(date_from=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert finance_payments.list_payments(later).total == 0
    payment_slips.confirm(s.id, WHO, amount_minor=2_000, account_id=s.account_id, payer_reference=None, received_at=NOW)
    rows = finance_payments.list_payments(finance_payments.PaymentQuery()).rows
    assert len(rows) == 3 and sum(1 for r in rows if r["slip_id"] == s.id) == 1  # the confirmed slip is its payment row


def test_refunds_due_lists_paid_bookings_that_cant_travel(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)
    pay(reference, 46_700)
    connection.execute(text("UPDATE bookings SET booking_status='paid_not_ticketed', supplier_response = '{\"action_needed\": {\"reason\": \"hold_expired\"}}'"
                            " WHERE reference=:r"), {"r": reference})
    owed = finance_payments.refunds_due()
    assert [(r["booking_reference"], r["refundable_minor"], r["reason"]) for r in owed] == [(reference, 46_700, "hold_expired")]
    refund = finance.request_refund(reference, WHO, amount_minor=46_700, method="bank_transfer", reason="Not ticketed")
    assert finance_payments.refunds_due() == []  # an open refund takes it off the owed list
    finance.reject_refund(refund.id, WHO, "Wrong amount")
    assert len(finance_payments.refunds_due()) == 1


def test_settings_change_is_audited(database):
    connection, metadata, _ = database
    fresh(connection, metadata)
    finance.set_setting("card_fee_bps", 250, WHO, "New PayFast rate")
    assert finance.get_settings()["card_fee_bps"] == 250
    entry = finance.list_audit(action="setting_changed")[0][0]
    assert (entry.entity_id, entry.after, entry.reason) == ("card_fee_bps", {"value": 250}, "New PayFast rate")
