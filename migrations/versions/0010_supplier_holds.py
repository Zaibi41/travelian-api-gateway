"""Supplier holds on bookings, issuance "unknown", and a booking audit trail.

- bookings.supplier_ref: the supplier's ticketing reference (Southair ticket_ref).
  Server-side only; never returned by the API.
- bookings.supplier_response: the supplier's booking / fare / ticketing responses.
  Server-side only; never returned by the API.
- bookings.issuance_status may be 'unknown' (ticketing outcome couldn't be determined).
- booking_events: who did what to a booking, and when. No passenger data.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_supplier_holds"
down_revision = "0009_staff_role_permissions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("supplier_ref", sa.String(4096), nullable=True))
    op.add_column("bookings", sa.Column("supplier_response", postgresql.JSONB(), nullable=True))
    op.drop_constraint("ck_bookings_issuance", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_issuance", "bookings", "issuance_status IN ('not_issued', 'issued', 'unknown')")
    op.create_table(
        "booking_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_booking_events_booking", "booking_events", ["booking_id", "created_at"])


def downgrade():
    op.drop_index("ix_booking_events_booking", table_name="booking_events")
    op.drop_table("booking_events")
    op.execute("UPDATE bookings SET issuance_status = 'not_issued' WHERE issuance_status = 'unknown'")
    op.drop_constraint("ck_bookings_issuance", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_issuance", "bookings", "issuance_status IN ('not_issued', 'issued')")
    op.drop_column("bookings", "supplier_response")
    op.drop_column("bookings", "supplier_ref")
