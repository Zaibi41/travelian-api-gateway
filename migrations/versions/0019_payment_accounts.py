"""Travelian's receiving accounts, chosen when a customer payment is recorded.

- payment_accounts: the accounts customer payments arrive in (a bank account, a JazzCash or 1Link
  account), each with its payment method and a name staff recognise. Deactivated accounts stay
  (past payments point at them) but can't be chosen. Managed with app.cli.payment_accounts.
- booking_payments.account_id: the account a payment was recorded into. received_into keeps the
  account's name as it was at the time. NULL for payments recorded before this migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_payment_accounts"
down_revision = "0018_booking_itinerary"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "payment_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("method IN ('bank_transfer', 'jazzcash', '1link')", name="ck_payment_accounts_method"),
        sa.CheckConstraint("length(trim(label)) >= 2", name="ck_payment_accounts_label"),
    )
    op.create_index("uq_payment_accounts_active_label", "payment_accounts", ["method", sa.text("lower(label)")], unique=True,
                    postgresql_where=sa.text("active"))
    op.add_column("booking_payments", sa.Column("account_id", sa.String(36), sa.ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True))


def downgrade():
    op.drop_column("booking_payments", "account_id")
    op.drop_index("uq_payment_accounts_active_label", table_name="payment_accounts")
    op.drop_table("payment_accounts")
