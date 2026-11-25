"""Supplier types B2B portals and Hotels; B2B agency wallets (travelian issue 224, decision D3).

- suppliers.section gains 'b2b' (B2B flight / group ticketing portals not integrated by API, e.g. AeroGlobe) and
  'hotels' (hotel suppliers; RateHawk is booked by API, its balance is kept here until its API gives one).
- agency_wallet_entries: each B2B agency's prepaid wallet (D3: agencies only; B2C customers pay per booking).
  deposit (> 0, into a Travelian account) or adjustment (+/- with a reason). Append-only; the balance is the sum.
- agencies.wallet_low_minor: the agency's low-balance level (optional).
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_supplier_types_wallets"
down_revision = "0037_finance_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_suppliers_section", "suppliers", type_="check")
    op.create_check_constraint("ck_suppliers_section", "suppliers", "section IN ('api', 'direct', 'b2b', 'hotels')")
    op.execute("INSERT INTO suppliers (id, name, section, booking_mode, balance_mode) VALUES"
               " ('ratehawk', 'RateHawk', 'hotels', 'api', 'manual'), ('aeroglobe', 'AeroGlobe', 'b2b', 'manual', 'manual')"
               " ON CONFLICT DO NOTHING")

    op.add_column("agencies", sa.Column("wallet_low_minor", sa.BigInteger(), nullable=True))
    op.create_check_constraint("ck_agencies_wallet_low", "agencies", "wallet_low_minor IS NULL OR wallet_low_minor >= 0")
    op.create_table(
        "agency_wallet_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agency_id", sa.String(128), sa.ForeignKey("agencies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="PKR"),
        sa.Column("reference", sa.String(120), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("kind IN ('deposit', 'adjustment')", name="ck_agency_wallet_kind"),
        sa.CheckConstraint("(kind = 'deposit' AND amount_minor > 0) OR (kind = 'adjustment' AND amount_minor <> 0)", name="ck_agency_wallet_amount"),
        sa.CheckConstraint("kind <> 'adjustment' OR reason IS NOT NULL", name="ck_agency_wallet_reason"),
    )
    op.create_index("ix_agency_wallet_entries_agency", "agency_wallet_entries", ["agency_id", "occurred_at"])
    op.execute("CREATE TRIGGER agency_wallet_entries_append_only BEFORE UPDATE OR DELETE ON agency_wallet_entries"
               " FOR EACH ROW EXECUTE FUNCTION finance_append_only()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS agency_wallet_entries_append_only ON agency_wallet_entries")
    op.drop_index("ix_agency_wallet_entries_agency", table_name="agency_wallet_entries")
    op.drop_table("agency_wallet_entries")
    op.drop_constraint("ck_agencies_wallet_low", "agencies", type_="check")
    op.drop_column("agencies", "wallet_low_minor")
    op.execute("DELETE FROM suppliers s WHERE s.id IN ('ratehawk', 'aeroglobe') AND s.section IN ('b2b', 'hotels')"
               " AND NOT EXISTS (SELECT 1 FROM supplier_wallet_entries e WHERE e.provider_id = s.id)")
    op.execute("UPDATE suppliers SET section = 'direct' WHERE section IN ('b2b', 'hotels')")
    op.drop_constraint("ck_suppliers_section", "suppliers", type_="check")
    op.create_check_constraint("ck_suppliers_section", "suppliers", "section IN ('api', 'direct')")
