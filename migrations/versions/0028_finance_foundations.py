"""Finance foundations (Zaibi41/travelian#91).

- Permission "finance" (super_admin always; grantable to staff). Finance screens and actions need it.
- finance_audit_log: every finance action, who / when / what / before / after / reason. Append-only.
- payment_fees: what a payment cost Travelian (e.g. the PayFast fee), one row per recording. A correction is a new
  row (negative amount allowed); rows are never changed or deleted.
- supplier_charges: what the supplier actually charged for each ticket (per traveler where known). Recorded at issue
  from the booking's confirmed cost, or by Finance for tickets issued outside the gateway. Corrections are new rows.
- refunds: amount, method, reference, status (requested -> approved -> paid, or rejected), reason, and who did each
  step and when. The amount and booking never change after the request (trigger); status moves forward only.
- finance_settings: settings Finance changes (card fee rate, thresholds), each change audited.
- bookings.cost_known: false for bookings that already exist where the supplier cost equals the selling price
  (migration 0021 filled cost = selling for bookings without a cost). Their profit shows "not known", never 0.

All amounts are integer paisa (minor units of the booking currency, PKR). Triggers refuse UPDATE/DELETE on the
append-only tables, so a money record can only be corrected by adding another one.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028_finance_foundations"
down_revision = "0027_booking_is_test"
branch_labels = None
depends_on = None

OLD_PERMISSIONS = ("permission IN ('see_supplier_names', 'see_cost_and_margin', 'reveal_identity', 'record_payments', 'issue_tickets',"
                   " 'manage_people', 'merge_people')")
NEW_PERMISSIONS = ("permission IN ('see_supplier_names', 'see_cost_and_margin', 'reveal_identity', 'record_payments', 'issue_tickets',"
                   " 'manage_people', 'merge_people', 'finance')")

APPEND_ONLY = ("finance_audit_log", "payment_fees", "supplier_charges")


def _permissions(check: str) -> None:
    for table in ("role_permissions", "user_permissions"):
        op.drop_constraint(f"ck_{table}_permission", table, type_="check")
        op.create_check_constraint(f"ck_{table}_permission", table, check)


def _user(column: str, nullable: bool = True) -> sa.Column:
    return sa.Column(column, sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=nullable)


def upgrade() -> None:
    _permissions(NEW_PERMISSIONS)

    op.create_table(
        "finance_audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        _user("actor_user_id"),
        sa.Column("actor_role", sa.String(32), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(40), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_finance_audit_log_at", "finance_audit_log", ["at"])
    op.create_index("ix_finance_audit_log_entity", "finance_audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_finance_audit_log_booking", "finance_audit_log", ["booking_id"])

    op.create_table(
        "payment_fees",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("payment_id", sa.String(36), sa.ForeignKey("booking_payments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        # recorded: entered by Finance; estimated: from the agreed rate until settlement data exists; settlement: from
        # the provider's settlement file (Phase 4).
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _user("recorded_by_user_id"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("amount_minor <> 0", name="ck_payment_fees_amount"),
        sa.CheckConstraint("source IN ('recorded', 'estimated', 'settlement')", name="ck_payment_fees_source"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_payment_fees_currency"),
    )
    op.create_index("ix_payment_fees_payment", "payment_fees", ["payment_id"])

    op.create_table(
        "supplier_charges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("traveler_id", sa.String(36), sa.ForeignKey("booking_travelers.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("provider_id", sa.String(64), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        # issuance: the confirmed cost when the gateway recorded the ticket; manual: entered by Finance (tickets issued
        # in a supplier portal, or a correction); supplier_report: from a supplier statement (Phase 4).
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("supplier_reference", sa.String(120), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        _user("recorded_by_user_id"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("amount_minor <> 0", name="ck_supplier_charges_amount"),
        sa.CheckConstraint("source IN ('issuance', 'manual', 'supplier_report')", name="ck_supplier_charges_source"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_supplier_charges_currency"),
    )
    op.create_index("ix_supplier_charges_booking", "supplier_charges", ["booking_id"])

    op.create_table(
        "refunds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="requested"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payout_reference", sa.String(120), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        _user("requested_by_user_id"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        _user("approved_by_user_id"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        _user("paid_by_user_id"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        _user("rejected_by_user_id"),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_minor > 0", name="ck_refunds_amount"),
        sa.CheckConstraint("status IN ('requested', 'approved', 'paid', 'rejected')", name="ck_refunds_status"),
        sa.CheckConstraint("method IN ('bank_transfer', 'jazzcash', '1link', 'payfast')", name="ck_refunds_method"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_refunds_currency"),
        sa.CheckConstraint("status <> 'paid' OR (paid_at IS NOT NULL AND payout_reference IS NOT NULL)", name="ck_refunds_paid"),
    )
    op.create_index("ix_refunds_booking", "refunds", ["booking_id"])
    op.create_index("ix_refunds_status", "refunds", ["status", "requested_at"])

    op.create_table(
        "finance_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        _user("updated_by_user_id"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.add_column("bookings", sa.Column("cost_known", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.execute("UPDATE bookings SET cost_known = false WHERE supplier_cost_minor = total_minor")

    # Money records are never changed or deleted: corrections are new rows.
    op.execute("""
        CREATE FUNCTION finance_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are never changed or deleted; add a correcting entry instead', TG_TABLE_NAME;
        END $$""")
    for table in APPEND_ONLY:
        op.execute(f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}"
                   " FOR EACH ROW EXECUTE FUNCTION finance_append_only()")
    # A refund keeps its booking, amount, currency, method and reason; its status only moves forward.
    op.execute("""
        CREATE FUNCTION refunds_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'refunds are never deleted';
            END IF;
            IF NEW.booking_id <> OLD.booking_id OR NEW.amount_minor <> OLD.amount_minor OR NEW.currency <> OLD.currency
               OR NEW.method <> OLD.method OR NEW.reason <> OLD.reason OR NEW.requested_at <> OLD.requested_at THEN
                RAISE EXCEPTION 'a refund''s amount, method and reason never change; reject it and request a new one';
            END IF;
            IF NOT ((OLD.status = 'requested' AND NEW.status IN ('approved', 'rejected'))
                    OR (OLD.status = 'approved' AND NEW.status IN ('paid', 'rejected'))) THEN
                RAISE EXCEPTION 'refund status can''t go from % to %', OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER refunds_guard BEFORE UPDATE OR DELETE ON refunds FOR EACH ROW EXECUTE FUNCTION refunds_guard()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS refunds_guard ON refunds")
    op.execute("DROP FUNCTION IF EXISTS refunds_guard()")
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS finance_append_only()")
    op.drop_column("bookings", "cost_known")
    op.drop_table("finance_settings")
    op.drop_index("ix_refunds_status", table_name="refunds")
    op.drop_index("ix_refunds_booking", table_name="refunds")
    op.drop_table("refunds")
    op.drop_index("ix_supplier_charges_booking", table_name="supplier_charges")
    op.drop_table("supplier_charges")
    op.drop_index("ix_payment_fees_payment", table_name="payment_fees")
    op.drop_table("payment_fees")
    for index in ("ix_finance_audit_log_booking", "ix_finance_audit_log_entity", "ix_finance_audit_log_at"):
        op.drop_index(index, table_name="finance_audit_log")
    op.drop_table("finance_audit_log")
    for table in ("role_permissions", "user_permissions"):
        op.execute(f"DELETE FROM {table} WHERE permission = 'finance'")
    _permissions(OLD_PERMISSIONS)
