"""Bank Alfalah (Alfa Payment Gateway) online payments next to PayFast.

Additive: payfast_attempts (one row per online checkout started) gains `provider` ('payfast' for every existing row,
'alfalah' for APG); a payment recorded from a verified APG order status has method 'alfalah', and can be refunded the
same way.
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_alfalah_payments"
down_revision = "0035_document_issue_date"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payfast_attempts", sa.Column("provider", sa.String(12), nullable=False, server_default="payfast"))
    op.create_check_constraint("ck_payfast_attempts_provider", "payfast_attempts", "provider IN ('payfast', 'alfalah')")
    op.drop_constraint("ck_booking_payments_method", "booking_payments", type_="check")
    op.create_check_constraint("ck_booking_payments_method", "booking_payments",
                               "method IN ('bank_transfer', 'jazzcash', '1link', 'payfast', 'alfalah')")
    op.drop_constraint("ck_refunds_method", "refunds", type_="check")
    op.create_check_constraint("ck_refunds_method", "refunds", "method IN ('bank_transfer', 'jazzcash', '1link', 'payfast', 'alfalah')")


def downgrade():
    op.drop_constraint("ck_refunds_method", "refunds", type_="check")
    op.create_check_constraint("ck_refunds_method", "refunds", "method IN ('bank_transfer', 'jazzcash', '1link', 'payfast')")
    op.drop_constraint("ck_booking_payments_method", "booking_payments", type_="check")
    op.create_check_constraint("ck_booking_payments_method", "booking_payments",
                               "method IN ('bank_transfer', 'jazzcash', '1link', 'payfast')")
    op.drop_constraint("ck_payfast_attempts_provider", "payfast_attempts", type_="check")
    op.drop_column("payfast_attempts", "provider")
