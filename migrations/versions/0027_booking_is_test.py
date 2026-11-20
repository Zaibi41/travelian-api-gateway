"""Test bookings: Issue is refused on them (owner, 20 Nov 2026). A booking is a test when this flag is set or when a
passenger's first or last name is the word TEST (checked when issuing, so new test bookings need no flag). Set here for
TRV-G6CMP6, which the owner named as a test hold."""

import sqlalchemy as sa
from alembic import op

revision = "0027_booking_is_test"
down_revision = "0026_booking_view_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE bookings SET is_test = true WHERE reference = 'TRV-G6CMP6'")


def downgrade() -> None:
    op.drop_column("bookings", "is_test")
