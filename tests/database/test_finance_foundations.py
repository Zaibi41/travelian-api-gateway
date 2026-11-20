"""Finance foundations (migration 0028, travelian#91): permission, audit log, append-only money records, refunds,
supplier charges at issue, payment fees, cost_known and profit."""

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import text

from app.core.field_visibility import ACTIONS, FINANCE, resolve_actions
from app.db import booking_payments, finance, issuance
from app.db import bookings as store
from app.services import admin_bookings
from app.services.pricing import cost_unknown
from tests.database.test_booking_payments import account_for
from tests.database.test_booking_people import ACTOR, save

AT = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
WHO = finance.Actor(ACTOR, "super_admin")


def fresh(connection, metadata, **overrides):
    """A held booking (Rs 467 selling, Rs 457.70 cost) whose hold is still open."""
    return save(connection, metadata, hold_expires_at=datetime.now(timezone.utc) + timedelta(days=1), **overrides)


def pay(reference, amount, ref="TXN-1", method="bank_transfer"):
    payment, _ = booking_payments.record(reference, amount_minor=amount, currency="PKR", method=method, account_id=account_for(method),
                                         payer_reference=ref, received_at=AT, actor_user_id=ACTOR)
    return payment


def booking_id(connection, reference):
    return connection.scalar(text("SELECT id FROM bookings WHERE reference=:r"), {"r": reference})


def test_finance_permission_super_admin_always_staff_when_granted_never_agency():
    assert FINANCE in ACTIONS
    assert FINANCE in resolve_actions("super_admin", set())
    assert FINANCE not in resolve_actions("staff", set())
    assert FINANCE in resolve_actions("staff", {"finance"})
    assert resolve_actions("agency_owner", {"finance"}) == frozenset()


def test_finance_permission_can_be_granted_in_the_database(database):
    connection, metadata, _ = database
    fresh(connection, metadata)  # creates the ACTOR user
    connection.execute(text("INSERT INTO user_permissions (user_id, permission, reason) VALUES (:u, 'finance', 'test')"), {"u": ACTOR})


def test_money_records_and_audit_log_are_append_only(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)
    payment = pay(reference, 10_000)
    finance.add_payment_fee(payment.id, WHO, amount_minor=250, reason="PayFast statement")
    finance.add_supplier_charge(reference, WHO, amount_minor=45_770, reason="Issued in the portal")
    for table in ("payment_fees", "supplier_charges", "finance_audit_log"):
        with pytest.raises(sa.exc.DBAPIError, match="never changed or deleted"):
            with connection.begin_nested():
                connection.execute(text(f"UPDATE {table} SET reason = 'edited'" if table != "finance_audit_log" else f"UPDATE {table} SET reason = 'x'"))
        with pytest.raises(sa.exc.DBAPIError, match="never changed or deleted"):
            with connection.begin_nested():
                connection.execute(text(f"DELETE FROM {table}"))


def test_supplier_charge_correction_is_a_new_row_and_audited(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)
    finance.add_supplier_charge(reference, WHO, amount_minor=45_770, reason="SooperFare portal", supplier_reference="SF-1")
    finance.add_supplier_charge(reference, WHO, amount_minor=-2_000, reason="Portal shows Rs 437.70")
    charges = finance.supplier_charges_for(booking_id(connection, reference))
    assert [c.amount_minor for c in charges] == [45_770, -2_000]
    with pytest.raises(finance.FinanceRefused, match="below zero"):
        finance.add_supplier_charge(reference, WHO, amount_minor=-50_000, reason="too much")
    with pytest.raises(finance.FinanceRefused, match="why"):
        finance.add_supplier_charge(reference, WHO, amount_minor=100, reason=" ")
    entries, total = finance.list_audit(booking_reference=reference)
    assert total == 2 and entries[0].after["charges_total_minor"] == 43_770 and entries[0].reason == "Portal shows Rs 437.70"
    assert entries[0].actor_user_id == ACTOR and entries[0].actor_role == "super_admin"


def test_issue_records_actual_charge_per_ticket_once(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)
    bid = booking_id(connection, reference)
    connection.execute(text("UPDATE bookings SET issuance_status = 'processing' WHERE id = :b"), {"b": bid})
    target = issuance._load(connection, reference, lock=False)
    issuance.mark_issued(target, ACTOR, {0: "2141234567890"}, "issuance_issued", {})
    charges = finance.supplier_charges_for(bid)
    assert sum(c.amount_minor for c in charges) == 45_770 and {c.source for c in charges} == {"issuance"}
    assert [c.traveler_position for c in charges] == sorted(c.traveler_position for c in charges)
    # A second recording (ticket edits, a re-run) adds nothing.
    assert finance.record_issue_charges(connection, bid, ACTOR) == 0


def test_refund_lifecycle_and_guards(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)
    pay(reference, 46_700)
    with pytest.raises(finance.FinanceRefused, match="more than can be refunded"):
        finance.request_refund(reference, WHO, amount_minor=46_701, method="bank_transfer", reason="Flight cancelled")
    refund = finance.request_refund(reference, WHO, amount_minor=40_000, method="bank_transfer", reason="Flight cancelled")
    assert refund.status == "requested" and refund.requested_by_user_id == ACTOR
    with pytest.raises(finance.FinanceRefused, match="can't be marked paid"):
        finance.mark_refund_paid(refund.id, WHO, payout_reference="IBFT-1", paid_at=AT)
    approved = finance.approve_refund(refund.id, WHO)
    assert approved.status == "approved" and approved.approved_at is not None
    paid = finance.mark_refund_paid(refund.id, WHO, payout_reference="IBFT-1", paid_at=AT)
    assert paid.status == "paid" and paid.payout_reference == "IBFT-1" and paid.paid_by_user_id == ACTOR
    with pytest.raises(finance.FinanceRefused, match="paid and can't be marked rejected"):
        finance.reject_refund(refund.id, WHO, "changed mind")
    # Only Rs 67 left to refund; a rejected request frees its amount again.
    second = finance.request_refund(reference, WHO, amount_minor=6_700, method="jazzcash", reason="Seat fee")
    finance.reject_refund(second.id, WHO, "Not owed")
    assert finance.refundable_minor(connection, booking_id(connection, reference)) == 6_700
    # The database refuses changing the amount or going back, even bypassing the code.
    for sql in ("UPDATE refunds SET amount_minor = 1 WHERE id = :id", "UPDATE refunds SET status = 'requested' WHERE id = :id",
                "DELETE FROM refunds WHERE id = :id"):
        with pytest.raises(sa.exc.DBAPIError):
            with connection.begin_nested():
                connection.execute(text(sql), {"id": refund.id})
    actions = [e.action for e in finance.list_audit(booking_reference=reference)[0]]
    assert actions == ["refund_rejected", "refund_requested", "refund_paid", "refund_approved", "refund_requested"]


def test_payment_fee_needs_a_source_and_feeds_profit(database):
    connection, metadata, _ = database
    reference = fresh(connection, metadata)
    payment = pay(reference, 46_700)
    with pytest.raises(finance.FinanceRefused, match="where this fee comes from"):
        finance.add_payment_fee(payment.id, WHO, amount_minor=300)
    finance.add_payment_fee(payment.id, WHO, amount_minor=300, reason="Bank charge")
    from app.api.routes.finance import profit_view

    view = profit_view(finance.booking_finance(reference))
    # 46,700 selling - 45,770 quoted cost - 300 fee = 630
    assert view.profit.amount_minor == 630 and view.supplier_charge_basis == "quoted" and view.payment_fees.amount_minor == 300


def test_cost_known_marks_existing_cost_equals_selling_bookings(database):
    connection, metadata, config = database
    command.downgrade(config, "0027_booking_is_test")
    equal = fresh(connection, metadata, supplier_total_minor=46_700, pnr="EQ0001")
    real = fresh(connection, metadata, pnr="RL0001")
    command.upgrade(config, "head")
    known = dict(connection.execute(text("SELECT reference, cost_known FROM bookings")).all())
    assert known[equal] is False and known[real] is True
    rows = {row.booking_id: row for row in store.list_dashboard_bookings()}
    assert cost_unknown(None, rows[equal].supplier_cost_minor, rows[equal].total_minor, rows[equal].cost_known)
    assert not cost_unknown(None, rows[real].supplier_cost_minor, rows[real].total_minor, rows[real].cost_known)
    from app.core.field_visibility import ALL
    from app.db import booking_people

    detail = admin_bookings.build_detail(rows[equal], ALL, booking_people.read(equal))
    assert detail.fare.cost_unknown is True and detail.fare.margin is None
    # New bookings with selling == cost (zero margin) stay known.
    new = fresh(connection, metadata, supplier_total_minor=46_700, pnr="NW0001")
    assert connection.scalar(text("SELECT cost_known FROM bookings WHERE reference=:r"), {"r": new}) is True
    from app.api.routes.finance import profit_view

    assert profit_view(finance.booking_finance(equal)).profit is None
    assert profit_view(finance.booking_finance(equal)).supplier_charge_basis == "not_known"
