"""Booking notes (gateway#89): staff notes on a booking (text, who, when). Append-only; adding one is also logged on the
booking's audit trail (booking_events "note_added", without the text). Additive only."""

import sqlalchemy as sa
from alembic import op

revision = "0039_booking_notes"
down_revision = "0038_supplier_types_wallets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_notes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("body", sa.String(1000), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("length(btrim(body)) BETWEEN 1 AND 1000", name="ck_booking_notes_body"),
    )
    op.create_index("ix_booking_notes_booking", "booking_notes", ["booking_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_booking_notes_booking", table_name="booking_notes")
    op.drop_table("booking_notes")
