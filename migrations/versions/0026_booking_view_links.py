"""Signed links in customer emails: open one booking read-only without a login. Only the SHA-256 of the random token is
stored; a link expires at the later of 30 days after it was made and 2 days after the last travel date."""

import sqlalchemy as sa
from alembic import op

revision = "0026_booking_view_links"
down_revision = "0025_paid_not_ticketed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_view_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_booking_view_links_booking", "booking_view_links", ["booking_id"])


def downgrade() -> None:
    op.drop_index("ix_booking_view_links_booking", table_name="booking_view_links")
    op.drop_table("booking_view_links")
