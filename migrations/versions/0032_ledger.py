"""Double-entry ledger (travelian#72): chart of accounts, journals, journal lines, gaps.

- ledger_accounts: the chart of accounts. A standard set is seeded (system_key marks the ones automatic journals use);
  Finance can rename accounts, add new ones and deactivate non-system ones. Receiving accounts get their own bank
  ledger account (payment_account_id) when the ledger first posts to them.
- journals: one per event (payment confirmed, ticket issued, supplier charge, payment fee, refund paid, supplier
  top-up, adjustment), unique per source, plus manual journals with maker/checker approval (the approver is never the
  maker). A posted journal never changes; a correction is a new journal.
- journal_lines: debit or credit in integer paisa. Every journal balances (checked by the database when the
  transaction commits). Lines are never changed or deleted.
- ledger_gaps: what couldn't be posted from existing data (e.g. a ticket with no known supplier cost), shown to Finance.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_ledger"
down_revision = "0031_reconciliation"
branch_labels = None
depends_on = None

STANDARD = [
    ("1100", "Bank accounts (general)", "asset", "bank"),
    ("1150", "PayFast clearing", "asset", "payfast_clearing"),
    ("1310", "Southair account", "asset", "supplier:southair"),
    ("1320", "SooperFare wallet", "asset", "supplier:sooperfare"),
    ("1390", "Other supplier accounts", "asset", "supplier:other"),
    ("2100", "Customer deposits", "liability", "customer_deposits"),
    ("3000", "Owner's equity", "equity", "equity"),
    ("4000", "Ticket sales", "income", "sales"),
    ("4100", "Card fee income", "income", "card_fee_income"),
    ("4900", "Refunds to customers", "income", "refunds"),
    ("5000", "Cost of tickets", "expense", "cost_of_sales"),
    ("5100", "Payment processing fees", "expense", "payment_fees"),
    ("5200", "Supplier balance adjustments", "expense", "wallet_adjustments"),
]


def upgrade() -> None:
    accounts = op.create_table(
        "ledger_accounts",
        sa.Column("code", sa.String(10), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("type", sa.String(12), nullable=False),
        sa.Column("system_key", sa.String(60), nullable=True, unique=True),
        sa.Column("payment_account_id", sa.String(36), sa.ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("type IN ('asset', 'liability', 'equity', 'income', 'expense')", name="ck_ledger_accounts_type"),
        sa.CheckConstraint("code ~ '^[0-9]{4,10}$'", name="ck_ledger_accounts_code"),
    )
    op.bulk_insert(accounts, [{"code": c, "name": n, "type": t, "system_key": k} for c, n, t, k in STANDARD])
    op.create_table(
        "journals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("number", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("posted_on", sa.Date(), nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="posted"),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("approved_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("create_txid", sa.BigInteger(), nullable=False, server_default=sa.text("txid_current()")),
        sa.CheckConstraint("source_type IN ('payment', 'ticket', 'supplier_charge', 'payment_fee', 'refund', 'top_up', 'adjustment', 'manual')",
                           name="ck_journals_source"),
        sa.CheckConstraint("status IN ('posted', 'pending_approval', 'rejected')", name="ck_journals_status"),
        sa.CheckConstraint("source_type = 'manual' OR (status = 'posted' AND source_id IS NOT NULL)", name="ck_journals_auto"),
        sa.CheckConstraint("source_type <> 'manual' OR created_by_user_id IS NOT NULL", name="ck_journals_maker"),
        sa.CheckConstraint("source_type <> 'manual' OR status <> 'posted' OR (approved_by_user_id IS NOT NULL"
                           " AND approved_by_user_id IS DISTINCT FROM created_by_user_id)", name="ck_journals_checker"),
        sa.CheckConstraint("status <> 'rejected' OR rejected_reason IS NOT NULL", name="ck_journals_rejected"),
    )
    op.create_index("uq_journals_source", "journals", ["source_type", "source_id"], unique=True, postgresql_where=sa.text("source_type <> 'manual'"))
    op.create_index("ix_journals_posted_on", "journals", ["posted_on"])
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("journal_id", sa.String(36), sa.ForeignKey("journals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("account_code", sa.String(10), sa.ForeignKey("ledger_accounts.code", ondelete="RESTRICT"), nullable=False),
        sa.Column("debit_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credit_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("memo", sa.String(200), nullable=True),
        sa.CheckConstraint("debit_minor >= 0 AND credit_minor >= 0 AND (debit_minor = 0) <> (credit_minor = 0)", name="ck_journal_lines_side"),
    )
    op.create_index("ix_journal_lines_journal", "journal_lines", ["journal_id"])
    op.create_index("ix_journal_lines_account", "journal_lines", ["account_code"])
    op.create_table(
        "ledger_gaps",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("source_type", "source_id", "reason", name="uq_ledger_gaps"),
    )
    op.execute("""
        CREATE FUNCTION journal_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE difference bigint;
        BEGIN
            SELECT COALESCE(SUM(debit_minor), 0) - COALESCE(SUM(credit_minor), 0) INTO difference FROM journal_lines WHERE journal_id = NEW.journal_id;
            IF difference <> 0 THEN RAISE EXCEPTION 'journal % doesn''t balance (debits - credits = %)', NEW.journal_id, difference; END IF;
            RETURN NULL;
        END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER journal_lines_balanced AFTER INSERT ON journal_lines DEFERRABLE INITIALLY DEFERRED"
               " FOR EACH ROW EXECUTE FUNCTION journal_balanced()")
    op.execute("CREATE TRIGGER journal_lines_append_only BEFORE UPDATE OR DELETE ON journal_lines FOR EACH ROW EXECUTE FUNCTION finance_append_only()")
    op.execute("""
        CREATE FUNCTION journals_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'journals are never deleted; post a correcting journal'; END IF;
            IF OLD.status <> 'pending_approval' OR NEW.status NOT IN ('posted', 'rejected') THEN
                RAISE EXCEPTION 'journal can''t go from % to %; post a correcting journal', OLD.status, NEW.status;
            END IF;
            IF NEW.id <> OLD.id OR NEW.posted_on <> OLD.posted_on OR NEW.description <> OLD.description OR NEW.source_type <> OLD.source_type
               OR NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.booking_id IS DISTINCT FROM OLD.booking_id
               OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id OR NEW.number <> OLD.number
               OR NEW.created_at <> OLD.created_at OR NEW.create_txid <> OLD.create_txid THEN
                RAISE EXCEPTION 'a journal''s content never changes';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER journals_guard BEFORE UPDATE OR DELETE ON journals FOR EACH ROW EXECUTE FUNCTION journals_guard()")
    op.execute("""
        CREATE FUNCTION journals_stamp() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.create_txid := txid_current();
            NEW.created_at := clock_timestamp();
            RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER journals_stamp BEFORE INSERT ON journals FOR EACH ROW EXECUTE FUNCTION journals_stamp()")
    op.execute("""
        CREATE FUNCTION journal_lines_open_journal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- Lines are written only by the transaction that created the journal (create_txid is set by the database).
            IF (SELECT create_txid FROM journals WHERE id = NEW.journal_id) IS DISTINCT FROM txid_current() THEN
                RAISE EXCEPTION 'lines can only be added when the journal is created';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER journal_lines_open_journal BEFORE INSERT ON journal_lines FOR EACH ROW EXECUTE FUNCTION journal_lines_open_journal()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS journal_lines_open_journal ON journal_lines")
    op.execute("DROP FUNCTION IF EXISTS journal_lines_open_journal()")
    op.execute("DROP TRIGGER IF EXISTS journals_stamp ON journals")
    op.execute("DROP FUNCTION IF EXISTS journals_stamp()")
    op.execute("DROP TRIGGER IF EXISTS journals_guard ON journals")
    op.execute("DROP FUNCTION IF EXISTS journals_guard()")
    op.execute("DROP TRIGGER IF EXISTS journal_lines_append_only ON journal_lines")
    op.execute("DROP TRIGGER IF EXISTS journal_lines_balanced ON journal_lines")
    op.execute("DROP FUNCTION IF EXISTS journal_balanced()")
    op.drop_table("ledger_gaps")
    op.drop_index("ix_journal_lines_account", table_name="journal_lines")
    op.drop_index("ix_journal_lines_journal", table_name="journal_lines")
    op.drop_table("journal_lines")
    op.drop_index("ix_journals_posted_on", table_name="journals")
    op.drop_index("uq_journals_source", table_name="journals")
    op.drop_table("journals")
    op.drop_table("ledger_accounts")
