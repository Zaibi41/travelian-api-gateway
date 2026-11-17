"""Public website booking: guest bookings and PayFast payments.

- bookings.channel: 'staff' (made in the admin) or 'website' (made by a guest on the public site).
- bookings.guest_token_hash: SHA-256 of the private token a guest uses to view and pay their booking.
  The token itself is never stored.
- booking_payments.method gains 'payfast' (online card / wallet payments, no receiving account).
- payfast_attempts: one row per PayFast checkout started (basket id, amount, outcome). A payment is
  recorded only from a notification the gateway verified (validation hash with the secured key, amount).
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_public_booking"
down_revision = "0021_selling_price"
branch_labels = None
depends_on = None


STATUSES = ("'held', 'pending_payment', 'payment_received', 'confirmed', 'ticket_issued', 'cancelled', 'expired',"
            " 'hold_pending', 'hold_failed', 'hold_unrecorded'")


def upgrade() -> None:
    # 'requested': a website booking request while live booking is off (no supplier call, no payment); staff follow up.
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", f"booking_status IN ({STATUSES}, 'requested')")
    op.add_column("bookings", sa.Column("channel", sa.String(16), nullable=False, server_default="staff"))
    op.create_check_constraint("ck_bookings_channel", "bookings", "channel IN ('staff', 'website')")
    op.add_column("bookings", sa.Column("guest_token_hash", sa.String(64), nullable=True))
    op.drop_constraint("ck_booking_payments_method", "booking_payments", type_="check")
    op.create_check_constraint("ck_booking_payments_method", "booking_payments",
                               "method IN ('bank_transfer', 'jazzcash', '1link', 'payfast')")
    op.create_table(
        "payfast_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("basket_id", sa.String(48), nullable=False, unique=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="initiated"),
        sa.Column("transaction_id", sa.String(64), nullable=True),
        sa.Column("err_code", sa.String(8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('initiated', 'paid', 'failed')", name="ck_payfast_attempts_status"),
        sa.CheckConstraint("amount_minor > 0", name="ck_payfast_attempts_amount"),
    )
    op.create_index("ix_payfast_attempts_booking", "payfast_attempts", ["booking_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_payfast_attempts_booking", table_name="payfast_attempts")
    op.drop_table("payfast_attempts")
    op.execute("DELETE FROM booking_payments WHERE method = 'payfast'")
    op.drop_constraint("ck_booking_payments_method", "booking_payments", type_="check")
    op.create_check_constraint("ck_booking_payments_method", "booking_payments", "method IN ('bank_transfer', 'jazzcash', '1link')")
    op.drop_column("bookings", "guest_token_hash")
    op.drop_constraint("ck_bookings_channel", "bookings", type_="check")
    op.drop_column("bookings", "channel")
    op.execute("UPDATE bookings SET booking_status = 'cancelled' WHERE booking_status = 'requested'")
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", f"booking_status IN ({STATUSES})")
