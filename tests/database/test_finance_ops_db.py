"""Finance operations against PostgreSQL (migration 0037): the per-supplier ledger, the D52 negative-balance block on all three
deduction paths (Ticket Sale Entry, Supplier payment settlement, B28 automatic card issuance), refund credits (D56), the payment
difference (D57), Customer Dues and accounts (D54)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.db import booking_payments, public_bookings, finance, finance_ops as ops, issuance as issuance_store, payment_accounts, supplier_ledger as ledger, supplier_wallets
from app.services import finance_reports, flight_payments, issuance
from tests.database.test_booking_people import ACTOR, save
from tests.database.test_flight_payments_db import (  # noqa: F401 - fixtures
    admin_statuses, no_real_rehold, pay, production_payfast, signed, tickets_sent, website_hold,
)
from tests.database.test_issuance import FakeSouthair
from tests.southair.fixtures import pnr_info

pytestmark = pytest.mark.real_notifications

FIN = finance.Actor(ACTOR, "super_admin")
NOW = datetime.now(timezone.utc)
RS = 100  # paisa per rupee


@pytest.fixture(autouse=True)
def finance_user(request):
    """FIN's user row, which the finance audit log and balance snapshots reference."""
    if "database" in request.fixturenames:
        connection, metadata, _ = request.getfixturevalue("database")
        if not connection.execute(sa.text("SELECT 1 FROM users WHERE id=:id"), {"id": ACTOR}).first():
            connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))


def sale(**overrides) -> ops.SaleInput:
    values = dict(channel="direct", agency_id=None, customer_person_id=None, passenger_name="Muhammad Azam", travel_date=None, route="LHE-JED",
                  pnr="ABC123", supplier_id="airsial", cost_minor=60_000 * RS, sale_minor=62_000 * RS, received_minor=0, payment_account_id=None,
                  ticket_status="issued", notes=None)
    return ops.SaleInput(**{**values, **overrides})


def balance(supplier_id: str) -> int | None:
    return next(b for b in ledger.balances() if b.supplier.id == supplier_id).current_minor


def refused_code(fn, *args, **kwargs) -> str:
    with pytest.raises(finance.FinanceRefused) as error:
        fn(*args, **kwargs)
    return error.value.code


def booking_id(connection, reference: str) -> str:
    return connection.scalar(sa.text("SELECT id FROM bookings WHERE reference = :r"), {"r": reference})


def test_seeded_suppliers_in_two_sections(database):
    names = {(b.supplier.section, b.supplier.name) for b in ledger.balances()}
    assert ("api", "South Air") in names and ("api", "Sooperfare") in names
    assert {n for s, n in names if s == "direct"} == {"AirSial", "AirBlue", "Fly Jinnah / Air Arabia", "Salam Air", "Centrum Air", "Fly Nas",
                                                    "Fly Dubai", "PIA"}
    assert next(b for b in ledger.balances() if b.supplier.id == "pia").status == "not_set_up"


def test_one_ledger_shows_ticket_sales_and_real_booking_deductions_and_blocks_negative(database):
    """AirSial: opening Rs 100,000; a Ticket Sale Entry (Rs 60,000); a booking settled at its billable amount (Rs 30,000);
    then each deduction path refused at the point it would go below zero; a refund credit pays a settlement in part."""
    connection, metadata, _ = database
    ledger.set_opening(FIN, "airsial", 100_000 * RS, NOW - timedelta(days=1))
    first = ops.create_sale(FIN, sale(), finance_role=True)
    assert first.state == "confirmed" and balance("airsial") == 40_000 * RS

    # Path 1: Ticket Sale Entry. Finance save refused; a staff submission is saved pending and its confirmation refused.
    assert refused_code(ops.create_sale, FIN, sale(cost_minor=50_000 * RS, sale_minor=51_000 * RS), finance_role=True) == ledger.NEGATIVE
    pending = ops.create_sale(finance.Actor(ACTOR, "staff"), sale(cost_minor=50_000 * RS, sale_minor=51_000 * RS), finance_role=False)
    assert pending.state == "pending" and balance("airsial") == 40_000 * RS  # nothing deducted until Finance confirms
    # Staff without Finance aren't shown supplier balance figures, only that Finance can't confirm it yet.
    staff_view = ops.preview_sale(sale(cost_minor=50_000 * RS, sale_minor=51_000 * RS), confirmed=False)
    assert not staff_view.problems and not any("40,000" in e or "-10,000" in e for e in staff_view.effects)
    # ...and the same whatever the cost, so a submission can't be used to probe the balance.
    cheap = ops.preview_sale(sale(cost_minor=1 * RS, sale_minor=51_000 * RS), confirmed=False)
    assert [e for e in cheap.effects if "balance" in e] == [e for e in staff_view.effects if "balance" in e]
    assert refused_code(ops.confirm_sale, FIN, pending.id) == ledger.NEGATIVE
    # An adjustment can't take it negative either (the balance only moves the three ways).
    assert refused_code(ledger.add_top_up, FIN, "airsial", kind="adjustment", amount_minor=-50_000 * RS, reference=None, reason="x",
                        occurred_at=NOW, paid_from_account_id=None) == ledger.NEGATIVE

    # Path 2: Supplier payment settlement (a booking issued off-system through AirSial).
    one = save(connection, metadata, pnr="AAA111")
    ops.set_billable(FIN, one, billable_minor=30_000 * RS, supplier_id="airsial", note="Quoted by AirSial")
    ops.settle(FIN, one, method="fresh", refund_id=None)
    assert balance("airsial") == 10_000 * RS
    assert finance.record_issue_charges(connection, booking_id(connection, one), ACTOR) == 0  # settled: issuance adds no second charge
    two = save(connection, metadata, pnr="BBB222")
    ops.set_billable(FIN, two, billable_minor=20_000 * RS, supplier_id="airsial", note=None)
    assert refused_code(ops.settle, FIN, two, method="fresh", refund_id=None) == ledger.NEGATIVE

    # D56: a Rs 15,000 refund credit pays most of it; the balance pays Rs 5,000.
    refund = ops.open_refund(FIN, supplier_id="airsial", booking_reference=None, reference="RF-1", amount_minor=15_000 * RS, reason="Cancelled PNR",
                             opened_on=None)
    settled = ops.settle(FIN, two, method="refund_credit", refund_id=refund.id)
    assert settled.credit_used_minor == 15_000 * RS and balance("airsial") == 5_000 * RS
    used = next(r for r in ops.list_refunds() if r.id == refund.id)
    assert used.remaining_minor == 0 and used.status == "received"

    # The one history: opening, the Ticket Sale Entry, both settlements and the refund credit, each with its cause.
    _, b, items = ledger.ledger("airsial")
    kinds = [(i.kind, i.amount_minor) for i in items]
    assert kinds == [("opening", 100_000 * RS), ("ticket_sale", -60_000 * RS), ("booking_charge", -30_000 * RS),
                     ("refund_credit", 15_000 * RS), ("booking_charge", -20_000 * RS)]
    assert items[1].ticket_sale_number == first.number and "Ticket Sale Entry" in items[1].description
    assert items[2].booking_reference == one and "Supplier payment settlement" in items[2].description
    assert items[-1].balance_minor == b.current_minor == 5_000 * RS
    # The booking's supplier charge is the billable amount (the Fare tab's figure), and the profit uses it.
    profit = finance_reports.profits([r for r in __import__("app.db.bookings", fromlist=["x"]).list_dashboard_bookings() if r.booking_id == one])[one]
    assert (profit.supplier_charge_minor, profit.supplier_charge_basis) == (30_000 * RS, "billable")


def test_b28_card_issuance_against_a_low_balance_is_paid_issuance_pending_then_issues_after_top_up(website_hold, no_real_rehold):  # noqa: F811
    """D52 on the automatic path (B28): the card payment is verified and recorded; Southair's balance (Rs 100) can't cover the
    ticket (Rs 457.70), so nothing is sent for ticketing and the booking is 'Paid, issuance pending' with staff alerted (D48).
    After the top-up the same path issues it, and the charge is on South Air's ledger as issued automatically."""
    connection, reference = website_hold
    public_bookings.create_attempt(reference, "TRV-F-LOW", 46_700, "PKR")
    with patch.object(settings, "southair_ticketing_enabled", True):
        assert pay(reference, "TRV-F-LOW", signed("TRV-F-LOW", transaction_id="T1", transaction_amount="467.00"), []).status == "paid"
        low = FakeSouthair([pnr_info(), pnr_info()], balance="100.00")
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: low)) == "needs_staff"
        assert tickets_sent(low) == 0
        assert admin_statuses(reference) == ("paid_issuance_pending", "paid", "not_issued")
        reason = connection.scalar(sa.text("SELECT details ->> 'reason_code' FROM booking_events e JOIN bookings b ON b.id = e.booking_id"
                                           " WHERE b.reference = :r AND e.action = 'issuance_aborted' ORDER BY e.created_at DESC LIMIT 1"), {"r": reference})
        assert reason == "insufficient_balance"
        assert connection.scalar(sa.text("SELECT COALESCE(SUM(amount_minor), 0) FROM booking_payments p JOIN bookings b ON b.id = p.booking_id"
                                         " WHERE b.reference = :r"), {"r": reference}) == 46_700  # the payment stands
        topped_up = FakeSouthair([pnr_info(), __import__("tests.database.test_issuance", fromlist=["ISSUED"]).ISSUED], balance="100000.00")
        assert asyncio.run(flight_payments.after_verified_card_payment(reference, client_factory=lambda: topped_up)) == "issued"
    _, _, items = ledger.ledger("southair")
    auto = [i for i in items if i.booking_reference == reference]
    assert auto and all("issued automatically" in i.description for i in auto)


def test_sooperfare_staff_issue_is_refused_below_zero(database):
    """D52 on the issuing path for a supplier with a calculated balance: refused before SooperFare's confirm (which charges)."""
    assert issuance.balance_shortfall("sooperfare", 50_000 * RS) is None  # balance not set up yet: not known, not refused
    ledger.set_opening(FIN, "sooperfare", 40_000 * RS, NOW - timedelta(hours=1))
    assert issuance.balance_shortfall("sooperfare", 40_000 * RS) is None
    message = issuance.balance_shortfall("sooperfare", 40_001 * RS)
    assert message and "Not enough supplier balance" in message
    # The check sits before the confirm call in the SooperFare issuing path.
    import inspect

    source = inspect.getsource(issuance._issue_sooperfare)
    assert source.index("balance_shortfall") < source.index("client.confirm(")
    # Finance > Wallets shows the same calculated balance.
    assert supplier_wallets.wallet("sooperfare").balance_minor == 40_000 * RS


def test_ticket_sale_on_southair_ledger_next_to_auto_issued_booking(database):
    connection, metadata, _ = database
    supplier_wallets.record_snapshot("southair", 500_000 * RS, "PKR", FIN)
    entry = ops.create_sale(FIN, sale(supplier_id="southair", cost_minor=1_000 * RS, sale_minor=1_200 * RS), finance_role=True)
    reference = save(connection, metadata, pnr="CCC333")
    finance.record_issue_charges(connection, booking_id(connection, reference), None)  # as at automatic issue (no user)
    _, b, items = ledger.ledger("southair")
    assert b.current_minor == 500_000 * RS  # Southair: its own system's balance
    assert any(i.kind == "ticket_sale" and i.ticket_sale_number == entry.number for i in items)
    assert any(i.kind == "booking_charge" and i.booking_reference == reference and "issued automatically" in i.description for i in items)
    assert refused_code(ops.create_sale, FIN, sale(supplier_id="southair", cost_minor=600_000 * RS, sale_minor=600_000 * RS),
                        finance_role=True) == ledger.NEGATIVE


def test_payment_difference_unlocks_issuance_and_counts_only_what_was_received(database):
    """D57 (issue #202: Rs 76,006 total, Rs 75,000 received): accepted with a reason, the booking counts as paid and issuance
    unlocks; profit uses what was received; the booking leaves Customer Dues."""
    connection, metadata, _ = database
    reference = save(connection, metadata, selling_total_minor=76_006 * RS, supplier_total_minor=74_886 * RS,
                     hold_expires_at=datetime.now(timezone.utc) + timedelta(hours=3))
    account = payment_accounts.add("bank_transfer", "UBL").id
    booking_payments.record(reference, amount_minor=75_000 * RS, currency="PKR", method="bank_transfer", account_id=account,
                            payer_reference="UBL-1", received_at=NOW, actor_user_id=ACTOR)
    target = issuance_store.load(reference)
    assert "Full payment isn't recorded yet." in issuance_store.blockers(target, datetime.now(timezone.utc), 0)
    assert any(d.reference == reference and d.due_minor == 1_006 * RS for d in ops.customer_dues())
    assert refused_code(ops.accept_shortfall, FIN, reference, reason_kind="channel_rate", note=" ") == "note_required"
    ops.accept_shortfall(FIN, reference, reason_kind="channel_rate", note="Reseller rate agreed on WhatsApp")
    target = issuance_store.load(reference)
    assert "Full payment isn't recorded yet." not in issuance_store.blockers(target, datetime.now(timezone.utc), 0)
    assert all(d.reference != reference for d in ops.customer_dues())
    ops.set_billable(FIN, reference, billable_minor=74_886 * RS, supplier_id="sooperfare", note="3% PIA discount")
    from app.db.bookings import list_dashboard_bookings

    profit = finance_reports.profits([r for r in list_dashboard_bookings() if r.booking_id == reference])[reference]
    assert profit.profit_minor == 114 * RS  # 75,000 received - 74,886 billed (issue #202's real profit)
    account_row = next(a for a in ops.account_balances() if a.id == account)
    assert account_row.sales_received_minor == 75_000 * RS


def test_accounts_business_and_personal(database):
    business = payment_accounts.add("bank_transfer", "UBL Business").id
    personal = payment_accounts.add("bank_transfer", "Owner HBL").id
    ops.update_account(FIN, personal, ownership="personal", opening_balance_minor=10_000 * RS)
    ops.update_account(FIN, business, ownership=None, opening_balance_minor=50_000 * RS)
    ops.create_sale(FIN, sale(supplier_id=None, received_minor=2_000 * RS, payment_account_id=business), finance_role=True)
    rows = {a.id: a for a in ops.account_balances()}
    assert rows[personal].ownership == "personal" and rows[personal].current_minor == 10_000 * RS
    assert rows[business].current_minor == 52_000 * RS and rows[business].ticket_sale_receipts_minor == 2_000 * RS
    dues = [d for d in ops.customer_dues() if d.kind == "ticket_sale"]
    assert dues and dues[0].due_minor == 60_000 * RS and dues[0].payment_account == "UBL Business"
