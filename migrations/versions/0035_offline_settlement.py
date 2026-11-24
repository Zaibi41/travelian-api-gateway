"""Bookings settled and ticketed outside Travelian (travelian#202, phase 1).

Additive only, all on bookings:
- Mark as issued (supplier with no ticketing from Travelian, e.g. SooperFare): how the supplier was paid for the ticket
  (supplier_paid_from: bank, supplier_wallet or credit), its transfer / confirmation reference and when it was sent, and
  the final ticket document (a private S3 key, ticket-documents/<uuid>.<ext>, and its content type).
- Settle the due: when the customer paid less than the booking total by agreement, the remaining due is waived with a
  reason. total_minor becomes what was paid; the original total, the waived amount, the reason, who and when are kept
  here so the Payments tab can show it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_offline_settlement"
down_revision = "0034_payment_account_logo"
branch_labels = None
depends_on = None


def _columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("supplier_paid_from", sa.String(16), nullable=True),
        sa.Column("supplier_payment_reference", sa.String(120), nullable=True),
        sa.Column("supplier_paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ticket_document_key", sa.String(300), nullable=True),
        sa.Column("ticket_document_type", sa.String(40), nullable=True),
        sa.Column("settled_waived_minor", sa.BigInteger(), nullable=True),
        sa.Column("settled_original_total_minor", sa.BigInteger(), nullable=True),
        sa.Column("settled_reason", sa.String(500), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
    )


def upgrade():
    for column in _columns():
        op.add_column("bookings", column)
    op.create_check_constraint("ck_bookings_supplier_paid_from", "bookings",
                               "supplier_paid_from IS NULL OR supplier_paid_from IN ('bank', 'supplier_wallet', 'credit')")
    op.create_check_constraint("ck_bookings_settled_waived", "bookings", "settled_waived_minor IS NULL OR settled_waived_minor > 0")


def downgrade():
    op.drop_constraint("ck_bookings_settled_waived", "bookings", type_="check")
    op.drop_constraint("ck_bookings_supplier_paid_from", "bookings", type_="check")
    for column in reversed(_columns()):
        op.drop_column("bookings", column.name)
