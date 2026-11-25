"""Whether the database has migration 0036 (Bank Alfalah) on payfast_attempts yet.

Migrations run by hand after a deploy (deploy/migrate.sh), so a new release can briefly run against the old schema. Until
0036 is applied the gateway writes PayFast attempts exactly as before and treats every attempt as real money (no sandbox
payment can exist then: a Bank Alfalah attempt needs the new columns). Once the column is seen it is remembered.
"""

from __future__ import annotations

from sqlalchemy import text

_has_columns = False


def has_alfalah_columns(connection) -> bool:
    """True once payfast_attempts has `provider` and `test_money` (migration 0036)."""
    global _has_columns
    if _has_columns:
        return True
    found = connection.scalar(text(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'payfast_attempts'"
        " AND column_name IN ('provider', 'test_money') AND table_schema = current_schema()"))
    _has_columns = found == 2
    return _has_columns


_has_finance_ops = False


def has_finance_ops(connection) -> bool:
    """True once migration 0037 (suppliers, Ticket Sale Entry, supplier refunds, booking supplier billing) is applied.
    Until then (migrations run by hand after a deploy) existing screens and issuing work exactly as before."""
    global _has_finance_ops
    if _has_finance_ops:
        return True
    _has_finance_ops = bool(connection.scalar(text(
        "SELECT to_regclass(current_schema() || '.booking_supplier_billing') IS NOT NULL")))
    return _has_finance_ops
