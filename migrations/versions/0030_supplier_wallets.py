"""Supplier wallets (travelian#68): balance readings, recorded top-ups and adjustments, low-balance alert state.

- supplier_balance_snapshots: each balance read from a supplier's API (Southair /my-profile limit.cap), with when and
  who asked (NULL = the scheduled check). Append-only.
- supplier_wallet_entries: money Travelian put into, or corrected on, a supplier account with no balance API
  (SooperFare): top_up (+) and adjustment (+/-), with reference, reason, who and when. Append-only. Tickets are not
  copied here: each issued ticket's actual charge is already a supplier_charges row (migration 0028), so the
  calculated balance = top-ups + adjustments - supplier charges for that supplier.
- supplier_wallet_alerts: per supplier, when the low-balance alert last fired (so it fires once per drop).
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_supplier_wallets"
down_revision = "0029_payments_slips_card_fee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supplier_balance_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="api"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("requested_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.CheckConstraint("source IN ('api')", name="ck_supplier_balance_snapshots_source"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_supplier_balance_snapshots_currency"),
    )
    op.create_index("ix_supplier_balance_snapshots_provider", "supplier_balance_snapshots", ["provider_id", "checked_at"])
    op.create_table(
        "supplier_wallet_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reference", sa.String(120), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("kind IN ('top_up', 'adjustment')", name="ck_supplier_wallet_entries_kind"),
        sa.CheckConstraint("amount_minor <> 0 AND (kind <> 'top_up' OR amount_minor > 0)", name="ck_supplier_wallet_entries_amount"),
        sa.CheckConstraint("kind <> 'adjustment' OR reason IS NOT NULL", name="ck_supplier_wallet_entries_reason"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_supplier_wallet_entries_currency"),
    )
    op.create_index("ix_supplier_wallet_entries_provider", "supplier_wallet_entries", ["provider_id", "occurred_at"])
    op.create_index("ix_supplier_charges_provider", "supplier_charges", ["provider_id"])
    op.create_table(
        "supplier_wallet_alerts",
        sa.Column("provider_id", sa.String(64), primary_key=True),
        sa.Column("low_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
    )
    for table in ("supplier_balance_snapshots", "supplier_wallet_entries"):
        op.execute(f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}"
                   " FOR EACH ROW EXECUTE FUNCTION finance_append_only()")


def downgrade() -> None:
    for table in ("supplier_balance_snapshots", "supplier_wallet_entries"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("supplier_wallet_alerts")
    op.drop_index("ix_supplier_charges_provider", table_name="supplier_charges")
    op.drop_index("ix_supplier_wallet_entries_provider", table_name="supplier_wallet_entries")
    op.drop_table("supplier_wallet_entries")
    op.drop_index("ix_supplier_balance_snapshots_provider", table_name="supplier_balance_snapshots")
    op.drop_table("supplier_balance_snapshots")
