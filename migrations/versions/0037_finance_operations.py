"""Finance operations: suppliers and their balances, Ticket Sale Entry, Supplier Refunds, booking supplier settlement.

Replaces the Excel workbook's supplier/airline-ID balances, ticket sales and refund credits. Additive only.

- suppliers: every supplier Travelian holds money with. section api (booking through an API: Southair, SooperFare) or
  direct (own airline IDs, prepaid float). balance_mode api (Southair: read from its API, never calculated) or manual
  (top-up log). settlement_tag (DTS / IATA) is a reference only. opening_balance_minor and opening_set_at start the
  calculated balance; low_threshold_minor is the low-balance level. Existing SooperFare/Southair thresholds are copied.
- supplier_wallet_entries gains paid_from_account_id (the bank account a top-up was paid from).
- ticket_sale_entries: off-system sales (not through the booking engine). Finance saves them confirmed; other staff
  submit them pending for Finance to confirm. A confirmed, issued entry with a supplier deducts that supplier's balance.
- supplier_refunds and supplier_refund_applications: money a supplier owes back (Requested -> Chased -> Received or
  Written off), and each time a refund was applied as credit against a booking's supplier charge.
- booking_supplier_billing: per booking, the supplier billable amount (the one figure the Fare and Payments tabs read)
  and how it was settled (fresh payment, or a refund credit).
- booking_payment_shortfalls: the booking total accepted as paid with less received, with the reason (discount,
  goodwill, negotiated channel rate).
- bookings.sold_through: the reseller or channel a booking was sold through (not the passenger or customer).
- payment_accounts gains ownership (business / personal; personal excluded from company totals) and opening balance.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_finance_operations"
down_revision = "0036_alfalah_payments"
branch_labels = None
depends_on = None

SUPPLIERS = [
    # id, name, section, booking_mode, balance_mode
    ("southair", "South Air", "api", "api", "api"),
    ("sooperfare", "Sooperfare", "api", "api", "manual"),
    ("airsial", "AirSial", "direct", "manual", "manual"),
    ("airblue", "AirBlue", "direct", "manual", "manual"),
    ("flyjinnah", "Fly Jinnah / Air Arabia", "direct", "manual", "manual"),
    ("salamair", "Salam Air", "direct", "manual", "manual"),
    ("centrumair", "Centrum Air", "direct", "manual", "manual"),
    ("flynas", "Fly Nas", "direct", "manual", "manual"),
    ("flydubai", "Fly Dubai", "direct", "manual", "manual"),
    ("pia", "PIA", "direct", "manual", "manual"),
]


def _user(column: str, nullable: bool = True) -> sa.Column:
    return sa.Column(column, sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=nullable)


def _at(column: str, nullable: bool = False) -> sa.Column:
    return sa.Column(column, sa.DateTime(timezone=True), nullable=nullable, server_default=None if nullable else sa.text("clock_timestamp()"))


def upgrade() -> None:
    op.create_table(
        "suppliers",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("section", sa.String(8), nullable=False),
        sa.Column("booking_mode", sa.String(8), nullable=False),
        sa.Column("balance_mode", sa.String(8), nullable=False),
        sa.Column("settlement_tag", sa.String(8), nullable=True),
        sa.Column("opening_balance_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("opening_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("low_threshold_minor", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _user("created_by_user_id"),
        _at("created_at"),
        sa.CheckConstraint("id ~ '^[a-z0-9][a-z0-9_-]{1,39}$'", name="ck_suppliers_id"),
        sa.CheckConstraint("section IN ('api', 'direct')", name="ck_suppliers_section"),
        sa.CheckConstraint("booking_mode IN ('api', 'manual')", name="ck_suppliers_booking_mode"),
        sa.CheckConstraint("balance_mode IN ('api', 'manual')", name="ck_suppliers_balance_mode"),
        sa.CheckConstraint("settlement_tag IS NULL OR settlement_tag IN ('DTS', 'IATA')", name="ck_suppliers_settlement_tag"),
        sa.CheckConstraint("low_threshold_minor IS NULL OR low_threshold_minor >= 0", name="ck_suppliers_threshold"),
    )
    table = sa.table("suppliers", sa.column("id"), sa.column("name"), sa.column("section"), sa.column("booking_mode"), sa.column("balance_mode"))
    op.bulk_insert(table, [dict(zip(("id", "name", "section", "booking_mode", "balance_mode"), row)) for row in SUPPLIERS])
    # The low-balance levels already set on Finance > Wallets (travelian#68) carry over.
    op.execute("UPDATE suppliers s SET low_threshold_minor = (f.value #>> '{}')::bigint FROM finance_settings f"
               " WHERE f.key = 'low_balance_' || s.id || '_minor' AND jsonb_typeof(f.value) = 'number'")

    op.add_column("supplier_wallet_entries", sa.Column("paid_from_account_id", sa.String(36),
                                                       sa.ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True))

    op.add_column("payment_accounts", sa.Column("ownership", sa.String(10), nullable=False, server_default="business"))
    op.add_column("payment_accounts", sa.Column("opening_balance_minor", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_payment_accounts_ownership", "payment_accounts", "ownership IN ('business', 'personal')")

    op.add_column("bookings", sa.Column("sold_through", sa.String(160), nullable=True))

    op.create_table(
        "ticket_sale_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("number", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("agency_id", sa.String(36), sa.ForeignKey("agencies.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("customer_person_id", sa.String(36), sa.ForeignKey("saved_people.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("passenger_name", sa.String(160), nullable=False),
        sa.Column("travel_date", sa.Date(), nullable=True),
        sa.Column("route", sa.String(80), nullable=True),
        sa.Column("pnr", sa.String(20), nullable=True),
        sa.Column("supplier_id", sa.String(40), sa.ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("cost_minor", sa.BigInteger(), nullable=False),
        sa.Column("sale_minor", sa.BigInteger(), nullable=False),
        sa.Column("received_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("payment_account_id", sa.String(36), sa.ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("ticket_status", sa.String(10), nullable=False, server_default="issued"),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        _user("submitted_by_user_id"),
        _at("submitted_at"),
        _user("confirmed_by_user_id"),
        _at("confirmed_at", nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("channel IN ('direct', 'b2b', 'other')", name="ck_tse_channel"),
        sa.CheckConstraint("channel <> 'b2b' OR agency_id IS NOT NULL", name="ck_tse_b2b_partner"),
        sa.CheckConstraint("cost_minor >= 0 AND sale_minor >= 0 AND received_minor >= 0", name="ck_tse_amounts"),
        sa.CheckConstraint("received_minor = 0 OR payment_account_id IS NOT NULL", name="ck_tse_received_account"),
        sa.CheckConstraint("ticket_status IN ('issued', 'on_hold')", name="ck_tse_ticket_status"),
        sa.CheckConstraint("state IN ('confirmed', 'pending', 'rejected')", name="ck_tse_state"),
        sa.CheckConstraint("state <> 'confirmed' OR confirmed_at IS NOT NULL", name="ck_tse_confirmed"),
        sa.CheckConstraint("state <> 'rejected' OR rejected_reason IS NOT NULL", name="ck_tse_rejected"),
    )
    op.create_index("ix_tse_supplier", "ticket_sale_entries", ["supplier_id", "confirmed_at"])
    op.create_index("ix_tse_state", "ticket_sale_entries", ["state", "submitted_at"])

    op.create_table(
        "supplier_refunds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("number", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("supplier_id", sa.String(40), sa.ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reference", sa.String(64), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="requested"),
        sa.Column("opened_on", sa.Date(), nullable=False, server_default=sa.text("CURRENT_DATE")),
        sa.Column("status_note", sa.Text(), nullable=True),
        _at("status_changed_at"),
        _user("created_by_user_id"),
        _at("created_at"),
        sa.CheckConstraint("amount_minor > 0", name="ck_supplier_refunds_amount"),
        sa.CheckConstraint("status IN ('requested', 'chased', 'received', 'written_off')", name="ck_supplier_refunds_status"),
    )
    op.create_index("ix_supplier_refunds_supplier", "supplier_refunds", ["supplier_id", "status"])
    op.create_table(
        "supplier_refund_applications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("refund_id", sa.String(36), sa.ForeignKey("supplier_refunds.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        _user("applied_by_user_id"),
        _at("applied_at"),
        sa.CheckConstraint("amount_minor > 0", name="ck_supplier_refund_applications_amount"),
    )
    op.create_index("ix_supplier_refund_applications_refund", "supplier_refund_applications", ["refund_id"])

    op.create_table(
        "booking_supplier_billing",
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("billable_minor", sa.BigInteger(), nullable=False),
        sa.Column("supplier_id", sa.String(40), sa.ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("settlement_method", sa.String(14), nullable=True),
        sa.Column("refund_id", sa.String(36), sa.ForeignKey("supplier_refunds.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("credit_used_minor", sa.BigInteger(), nullable=False, server_default="0"),
        _user("settled_by_user_id"),
        _at("settled_at", nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        _user("updated_by_user_id"),
        _at("updated_at"),
        sa.CheckConstraint("billable_minor > 0", name="ck_booking_supplier_billing_amount"),
        sa.CheckConstraint("settlement_method IS NULL OR settlement_method IN ('fresh', 'refund_credit')", name="ck_booking_supplier_billing_method"),
        sa.CheckConstraint("settlement_method <> 'refund_credit' OR refund_id IS NOT NULL", name="ck_booking_supplier_billing_refund"),
        sa.CheckConstraint("(settled_at IS NULL) = (settlement_method IS NULL)", name="ck_booking_supplier_billing_settled"),
    )

    op.create_table(
        "booking_payment_shortfalls",
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("reason_kind", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        _user("recorded_by_user_id"),
        _at("recorded_at"),
        sa.CheckConstraint("amount_minor > 0", name="ck_booking_payment_shortfalls_amount"),
        sa.CheckConstraint("reason_kind IN ('discount', 'goodwill', 'channel_rate')", name="ck_booking_payment_shortfalls_kind"),
    )
    # Money records are never changed or deleted (as the other finance records, migration 0028).
    for name in ("supplier_refund_applications", "booking_payment_shortfalls"):
        op.execute(f"CREATE TRIGGER {name}_append_only BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION finance_append_only()")


def downgrade() -> None:
    for name in ("supplier_refund_applications", "booking_payment_shortfalls"):
        op.execute(f"DROP TRIGGER IF EXISTS {name}_append_only ON {name}")
    op.drop_table("booking_payment_shortfalls")
    op.drop_table("booking_supplier_billing")
    op.drop_table("supplier_refund_applications")
    op.drop_table("supplier_refunds")
    op.drop_table("ticket_sale_entries")
    op.drop_column("bookings", "sold_through")
    op.drop_constraint("ck_payment_accounts_ownership", "payment_accounts", type_="check")
    op.drop_column("payment_accounts", "opening_balance_minor")
    op.drop_column("payment_accounts", "ownership")
    op.drop_column("supplier_wallet_entries", "paid_from_account_id")
    op.drop_table("suppliers")
