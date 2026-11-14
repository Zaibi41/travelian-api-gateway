"""Selling price on real PostgreSQL (skipped without TRAVELIAN_TEST_DATABASE_URL), plus an offline check of migration 0021.

Migration 0021: existing bookings keep any recorded supplier cost; a missing one becomes the current total (profit 0).
Travelers get their booking's totals split evenly. Columns are added nullable, backfilled, then made required.
No total, payment, received or due amount changes."""

from datetime import datetime, timezone
from io import StringIO

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.db import booking_payments, booking_pricing, payment_accounts

ACTOR = "22222222-2222-4222-8222-222222222222"


def test_migration_sql_adds_nullable_then_backfills_then_requires():
    output = StringIO()
    command.upgrade(Config("alembic.ini", output_buffer=output), "0020_group_hold_states:0021_selling_price", sql=True)
    sql = output.getvalue()
    steps = ["UPDATE bookings SET supplier_cost_minor = total_minor WHERE supplier_cost_minor IS NULL",
             "ALTER TABLE bookings ALTER COLUMN supplier_cost_minor SET NOT NULL",
             "ALTER TABLE booking_travelers ADD COLUMN selling_minor BIGINT",
             "UPDATE booking_travelers t",
             "ALTER TABLE booking_travelers ALTER COLUMN selling_minor SET NOT NULL"]
    positions = [sql.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "UPDATE bookings SET total_minor" not in sql and "booking_payments" not in sql  # no total or payment changes


def _booking(connection, reference, total, cost, travelers=2, status="held"):
    booking_id = f"00000000-0000-4000-8000-{reference[-12:]:0>12}"
    connection.execute(sa.text("INSERT INTO bookings (id, reference, product, booking_status, payment_status, issuance_status, currency, total_minor,"
                               " supplier_cost_minor) VALUES (:id, :r, 'group', :s, 'unpaid', 'not_issued', 'PKR', :t, :c)"),
                       {"id": booking_id, "r": reference, "s": status, "t": total, "c": cost})
    for position in range(travelers):
        connection.execute(sa.text("INSERT INTO booking_travelers (id, booking_id, position, passenger_type, title, first_name, last_name, date_of_birth, id_type)"
                                   " VALUES (:id, :b, :p, 'ADT', 'MR', 'Test', 'Traveler', '1990-01-01', 'PSPT')"),
                           {"id": f"{booking_id[:-2]}{position:02d}", "b": booking_id, "p": position})
    return booking_id


def test_migration_backfill_keeps_real_costs_and_fills_missing_ones(database):
    connection, _, config = database
    command.downgrade(config, "0020_group_hold_states")
    _booking(connection, "TRV-000000000001", 176_000_00, None)          # no cost: cost = total
    _booking(connection, "TRV-000000000002", 180_001, 170_000, travelers=2)  # a real cost: kept
    command.upgrade(config, "0021_selling_price")
    rows = connection.execute(sa.text("SELECT reference, total_minor, supplier_cost_minor FROM bookings ORDER BY reference")).all()
    assert [tuple(r) for r in rows] == [("TRV-000000000001", 176_000_00, 176_000_00), ("TRV-000000000002", 180_001, 170_000)]
    travelers = connection.execute(sa.text("SELECT b.reference, t.position, t.selling_minor, t.cost_minor FROM booking_travelers t"
                                           " JOIN bookings b ON b.id = t.booking_id ORDER BY b.reference, t.position")).all()
    assert [tuple(r) for r in travelers] == [("TRV-000000000001", 0, 88_000_00, 88_000_00), ("TRV-000000000001", 1, 88_000_00, 88_000_00),
                                             ("TRV-000000000002", 0, 90_001, 85_000), ("TRV-000000000002", 1, 90_000, 85_000)]


def test_selling_price_set_and_payments_capped_at_the_selling_total(database):
    connection, metadata, _ = database
    connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    booking_id = _booking(connection, "TRV-000000000003", 176_000_00, 176_000_00)
    connection.execute(sa.text("UPDATE booking_travelers SET selling_minor = 88_000_00, cost_minor = 88_000_00 WHERE booking_id = :b"), {"b": booking_id})
    with pytest.raises(booking_pricing.SellingRefused) as raised:
        booking_pricing.set_selling_price("TRV-000000000003", {"ADT": 87_900_00}, ACTOR)
    assert raised.value.code == "below_cost"
    result = booking_pricing.set_selling_price("TRV-000000000003", {"ADT": 88_200_00}, ACTOR, "PKR")
    assert (result.amounts.selling_total_minor, result.amounts.profit_minor) == (176_400_00, 400_00)
    account = payment_accounts.add("bank_transfer", "Meezan Bank, Travelian current account")
    pay = lambda amount, ref: booking_payments.record("TRV-000000000003", amount_minor=amount, currency="PKR", method="bank_transfer",  # noqa: E731
                                                       account_id=account.id, payer_reference=ref, received_at=datetime.now(timezone.utc), actor_user_id=ACTOR)
    with pytest.raises(booking_payments.PaymentRefused):
        pay(176_400_01, "TXN-1")  # above the selling total
    pay(176_400_00, "TXN-2")  # the full selling total (above the cost of 176,000)
    with pytest.raises(booking_pricing.SellingRefused) as raised:
        booking_pricing.set_selling_price("TRV-000000000003", {"ADT": 89_000_00}, ACTOR)
    assert raised.value.code == "payment_recorded"


def test_accepting_a_supplier_change_is_refused_below_cost(database):
    from app.db import group_holds

    connection, _, _ = database
    booking_id = _booking(connection, "TRV-GRP-000000000004", 180_000_00, 182_000_00)
    connection.execute(sa.text("UPDATE bookings SET supplier_response = CAST(:r AS jsonb) WHERE id = :b"),
                       {"r": '{"price_change": {"shown_cost_total_minor": 17600000, "held_cost_total_minor": 18200000, "accepted": false}}', "b": booking_id})
    with pytest.raises(group_holds.GroupHoldRefused) as raised:
        group_holds.accept_held_price("TRV-GRP-000000000004", ACTOR)
    assert raised.value.code == "below_cost"
