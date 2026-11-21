"""Reconciliation (travelian#71): imported statements, their lines, and one mismatch list.

- recon_imports: one uploaded file (PayFast settlement, bank statement for a receiving account, supplier statement),
  its column mapping, who and when. The same file (SHA-256) can't be imported twice for the same kind and account.
- recon_lines: each statement line with its amounts in integer paisa. A line's amounts never change (trigger); only
  its match status moves (suggested -> matched / unmatched, set by staff; PayFast lines match by transaction id).
- recon_mismatches: differences to work through. Status open -> explained / resolved, always with a reason, who and
  when. Nothing is changed in payments or bookings by reconciliation.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031_reconciliation"
down_revision = "0030_supplier_wallets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recon_imports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("provider_id", sa.String(64), nullable=True),
        sa.Column("file_name", sa.String(200), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("column_map", postgresql.JSONB(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("imported_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("kind IN ('payfast', 'bank', 'supplier')", name="ck_recon_imports_kind"),
        sa.CheckConstraint("kind <> 'bank' OR account_id IS NOT NULL", name="ck_recon_imports_account"),
    )
    op.create_index("uq_recon_imports_file", "recon_imports", ["kind", sa.text("COALESCE(account_id, '')"), sa.text("COALESCE(provider_id, '')"), "sha256"],
                    unique=True)
    op.create_table(
        "recon_lines",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("import_id", sa.String(36), sa.ForeignKey("recon_imports.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("occurred_on", sa.Date(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("fee_minor", sa.BigInteger(), nullable=True),
        sa.Column("net_minor", sa.BigInteger(), nullable=True),
        sa.Column("payout_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="unmatched"),
        sa.Column("payment_id", sa.String(36), sa.ForeignKey("booking_payments.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("matched_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('matched', 'suggested', 'unmatched', 'ignored')", name="ck_recon_lines_status"),
        sa.CheckConstraint("status <> 'matched' OR (matched_by_user_id IS NOT NULL AND matched_at IS NOT NULL"
                           " AND (payment_id IS NOT NULL OR booking_id IS NOT NULL))", name="ck_recon_lines_matched"),
    )
    op.create_index("ix_recon_lines_import", "recon_lines", ["import_id", "line_no"])
    op.create_index("ix_recon_lines_payment", "recon_lines", ["payment_id"])
    op.create_table(
        "recon_mismatches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False, unique=True),
        sa.Column("line_id", sa.String(36), sa.ForeignKey("recon_lines.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("payment_id", sa.String(36), sa.ForeignKey("booking_payments.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("expected_minor", sa.BigInteger(), nullable=True),
        sa.Column("actual_minor", sa.BigInteger(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("resolved_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("status IN ('open', 'explained', 'resolved')", name="ck_recon_mismatches_status"),
        sa.CheckConstraint("status = 'open' OR (resolution_reason IS NOT NULL AND resolved_at IS NOT NULL AND resolved_by_user_id IS NOT NULL)",
                           name="ck_recon_mismatches_reason"),
    )
    op.create_index("ix_recon_mismatches_status", "recon_mismatches", ["status", "created_at"])
    # Imports never change; a statement line keeps what the file said and is final once matched; a mismatch only moves
    # forward (open -> explained -> resolved) and keeps its facts. Nothing here is deleted.
    op.execute("""
        CREATE FUNCTION recon_lines_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'statement lines are never deleted'; END IF;
            IF NEW.id <> OLD.id OR NEW.import_id <> OLD.import_id OR NEW.line_no <> OLD.line_no OR NEW.amount_minor <> OLD.amount_minor
               OR NEW.fee_minor IS DISTINCT FROM OLD.fee_minor OR NEW.net_minor IS DISTINCT FROM OLD.net_minor
               OR NEW.reference IS DISTINCT FROM OLD.reference OR NEW.description IS DISTINCT FROM OLD.description
               OR NEW.occurred_on IS DISTINCT FROM OLD.occurred_on OR NEW.payout_on IS DISTINCT FROM OLD.payout_on THEN
                RAISE EXCEPTION 'a statement line keeps what the file said';
            END IF;
            IF OLD.status = 'matched' THEN RAISE EXCEPTION 'a matched statement line is final'; END IF;
            IF NOT (OLD.status = 'suggested' AND NEW.status IN ('matched', 'unmatched')) AND NEW.status <> OLD.status THEN
                RAISE EXCEPTION 'statement line can''t go from % to %', OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER recon_lines_guard BEFORE UPDATE OR DELETE ON recon_lines FOR EACH ROW EXECUTE FUNCTION recon_lines_guard()")
    op.execute("""
        CREATE FUNCTION recon_imports_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'imported statements are never changed or deleted'; END $$""")
    op.execute("CREATE TRIGGER recon_imports_guard BEFORE UPDATE OR DELETE ON recon_imports FOR EACH ROW EXECUTE FUNCTION recon_imports_guard()")
    op.execute("""
        CREATE FUNCTION recon_mismatches_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'mismatches are never deleted'; END IF;
            IF NEW.id <> OLD.id OR NEW.kind <> OLD.kind OR NEW.dedupe_key <> OLD.dedupe_key OR NEW.detail <> OLD.detail
               OR NEW.line_id IS DISTINCT FROM OLD.line_id OR NEW.payment_id IS DISTINCT FROM OLD.payment_id
               OR NEW.booking_id IS DISTINCT FROM OLD.booking_id OR NEW.expected_minor IS DISTINCT FROM OLD.expected_minor
               OR NEW.actual_minor IS DISTINCT FROM OLD.actual_minor OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION 'a mismatch keeps its facts';
            END IF;
            IF NOT ((OLD.status = 'open' AND NEW.status IN ('explained', 'resolved')) OR (OLD.status = 'explained' AND NEW.status = 'resolved')) THEN
                RAISE EXCEPTION 'mismatch status can''t go from % to %', OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER recon_mismatches_guard BEFORE UPDATE OR DELETE ON recon_mismatches FOR EACH ROW EXECUTE FUNCTION recon_mismatches_guard()")


def downgrade() -> None:
    for table in ("recon_imports", "recon_mismatches"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_guard ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_guard()")
    op.execute("DROP TRIGGER IF EXISTS recon_lines_guard ON recon_lines")
    op.execute("DROP FUNCTION IF EXISTS recon_lines_guard()")
    op.drop_index("ix_recon_mismatches_status", table_name="recon_mismatches")
    op.drop_table("recon_mismatches")
    op.drop_index("ix_recon_lines_payment", table_name="recon_lines")
    op.drop_index("ix_recon_lines_import", table_name="recon_lines")
    op.drop_table("recon_lines")
    op.drop_index("uq_recon_imports_file", table_name="recon_imports")
    op.drop_table("recon_imports")
