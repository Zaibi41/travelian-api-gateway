"""Customer payments on a booking; part-paid status; record_payments permission.

- booking_payments: amount (minor units, booking currency), method (bank transfer, JazzCash,
  1Link), the account it was received into, the payer's reference, when it was received, and
  who recorded it and when. A method + reference pair can be recorded once.
- bookings.payment_status gains 'part_paid' (column widened to fit it).
- Permission "record_payments" (grantable to staff; super_admin always).

Numbering: 0012 is reserved for role alignment. Alembic orders by down_revision, so 0012 will be
chained after the latest migration when it is written; the number is a label only.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_booking_payments"
down_revision = "0011_booking_people"
branch_labels = None
depends_on = None

OLD_PERMISSIONS = "permission IN ('see_supplier_names', 'see_cost_and_margin', 'reveal_identity')"
NEW_PERMISSIONS = "permission IN ('see_supplier_names', 'see_cost_and_margin', 'reveal_identity', 'record_payments')"


def _permissions(check: str) -> None:
    for table in ("role_permissions", "user_permissions"):
        op.drop_constraint(f"ck_{table}_permission", table, type_="check")
        op.create_check_constraint(f"ck_{table}_permission", table, check)


def upgrade():
    op.create_table(
        "booking_payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("received_into", sa.String(120), nullable=False),
        sa.Column("reference", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount_minor > 0", name="ck_booking_payments_amount"),
        sa.CheckConstraint("method IN ('bank_transfer', 'jazzcash', '1link')", name="ck_booking_payments_method"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_booking_payments_currency"),
    )
    op.create_index("ix_booking_payments_booking", "booking_payments", ["booking_id", "received_at"])
    op.create_index("uq_booking_payments_reference", "booking_payments", ["method", sa.text("lower(reference)")], unique=True)
    op.drop_constraint("ck_bookings_payment", "bookings", type_="check")
    op.alter_column("bookings", "payment_status", type_=sa.String(12))
    op.create_check_constraint("ck_bookings_payment", "bookings", "payment_status IN ('paid', 'unpaid', 'part_paid')")
    _permissions(NEW_PERMISSIONS)


def downgrade():
    for table in ("role_permissions", "user_permissions"):
        op.execute(f"DELETE FROM {table} WHERE permission = 'record_payments'")
    _permissions(OLD_PERMISSIONS)
    op.drop_constraint("ck_bookings_payment", "bookings", type_="check")
    op.execute("UPDATE bookings SET payment_status = 'unpaid' WHERE payment_status = 'part_paid'")
    op.alter_column("bookings", "payment_status", type_=sa.String(8))
    op.create_check_constraint("ck_bookings_payment", "bookings", "payment_status IN ('paid', 'unpaid')")
    op.drop_index("uq_booking_payments_reference", table_name="booking_payments")
    op.drop_index("ix_booking_payments_booking", table_name="booking_payments")
    op.drop_table("booking_payments")
