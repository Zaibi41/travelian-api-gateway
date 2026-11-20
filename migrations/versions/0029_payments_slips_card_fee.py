"""Payments: payment slips, the customer's chosen receiving account, card fee (travelian#69, #74, #76; gateway#56).

- payment_slips: a customer's "I've paid" for a bank transfer / JazzCash payment, with the slip file (private S3 key under
  payment-proof/), the account they paid into, amount and their reference. Status awaiting_check -> confirmed (staff
  record the payment; payment_id links it) or rejected (with a reason). The booking, amount and file never change
  (trigger); status only moves forward.
- bookings.chosen_account_id: the receiving account the customer picked at checkout (shown to staff, used to match).
- card_fee_minor on payfast_attempts and booking_payments: the card fee charged on top of the fare (integer paisa).
  PayFast is charged amount_minor (fare + fee) on the attempt; the payment's amount_minor is the fare part only, so
  amounts due, refunds and payment status are unchanged by the fee.
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_payments_slips_card_fee"
down_revision = "0028_finance_foundations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payfast_attempts", sa.Column("card_fee_minor", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("booking_payments", sa.Column("card_fee_minor", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_payfast_attempts_card_fee", "payfast_attempts", "card_fee_minor >= 0 AND card_fee_minor < amount_minor")
    op.create_check_constraint("ck_booking_payments_card_fee", "booking_payments", "card_fee_minor >= 0")
    op.add_column("bookings", sa.Column("chosen_account_id", sa.String(36), sa.ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True))

    op.create_table(
        "payment_slips",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("payer_reference", sa.String(64), nullable=True),
        sa.Column("asset_key", sa.String(300), nullable=False, unique=True),
        sa.Column("content_type", sa.String(40), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="awaiting_check"),
        sa.Column("submitted_via", sa.String(16), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("reviewed_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("payment_id", sa.String(36), sa.ForeignKey("booking_payments.id", ondelete="RESTRICT"), nullable=True, unique=True),
        sa.CheckConstraint("amount_minor > 0", name="ck_payment_slips_amount"),
        sa.CheckConstraint("method IN ('bank_transfer', 'jazzcash')", name="ck_payment_slips_method"),
        sa.CheckConstraint("status IN ('awaiting_check', 'confirmed', 'rejected')", name="ck_payment_slips_status"),
        sa.CheckConstraint("submitted_via IN ('checkout', 'my_bookings')", name="ck_payment_slips_via"),
        sa.CheckConstraint("content_type IN ('image/jpeg', 'image/png', 'image/webp', 'application/pdf')", name="ck_payment_slips_type"),
        sa.CheckConstraint("size_bytes > 0 AND size_bytes <= 5242880", name="ck_payment_slips_size"),
        sa.CheckConstraint("asset_key LIKE 'payment-proof/%'", name="ck_payment_slips_key"),
        sa.CheckConstraint("status <> 'confirmed' OR payment_id IS NOT NULL", name="ck_payment_slips_confirmed"),
        sa.CheckConstraint("status <> 'rejected' OR rejection_reason IS NOT NULL", name="ck_payment_slips_rejected"),
    )
    op.create_index("ix_payment_slips_booking", "payment_slips", ["booking_id"])
    op.create_index("ix_payment_slips_status", "payment_slips", ["status", "submitted_at"])
    op.execute("""
        CREATE FUNCTION payment_slips_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'payment slips are never deleted';
            END IF;
            IF NEW.booking_id <> OLD.booking_id OR NEW.amount_minor <> OLD.amount_minor OR NEW.asset_key <> OLD.asset_key
               OR NEW.method <> OLD.method OR NEW.submitted_at <> OLD.submitted_at THEN
                RAISE EXCEPTION 'a payment slip''s booking, amount, method and file never change';
            END IF;
            IF OLD.status <> 'awaiting_check' OR NEW.status NOT IN ('confirmed', 'rejected') THEN
                RAISE EXCEPTION 'payment slip status can''t go from % to %', OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER payment_slips_guard BEFORE UPDATE OR DELETE ON payment_slips FOR EACH ROW EXECUTE FUNCTION payment_slips_guard()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS payment_slips_guard ON payment_slips")
    op.execute("DROP FUNCTION IF EXISTS payment_slips_guard()")
    op.drop_index("ix_payment_slips_status", table_name="payment_slips")
    op.drop_index("ix_payment_slips_booking", table_name="payment_slips")
    op.drop_table("payment_slips")
    op.drop_column("bookings", "chosen_account_id")
    op.drop_constraint("ck_booking_payments_card_fee", "booking_payments", type_="check")
    op.drop_constraint("ck_payfast_attempts_card_fee", "payfast_attempts", type_="check")
    op.drop_column("booking_payments", "card_fee_minor")
    op.drop_column("payfast_attempts", "card_fee_minor")
