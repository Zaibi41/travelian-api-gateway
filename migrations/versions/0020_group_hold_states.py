"""Group holds: supplier PNRs as supplied, and the hold's own pending / failed / unrecorded states.

- bookings.pnr: up to 64 characters. A group booking's PNR is the supplier's and may contain '-', '/' or spaces
  (Skypass sends e.g. 4FGYK2-4B4VYB; Dhillu & Sahi e.g. "4FMUAF - 4KOCE5", kept exactly). The old rule (^[A-Z0-9]{1,16}$) refused it, so a Skypass hold
  succeeded at the supplier but our booking couldn't be saved (CheckViolation ck_bookings_pnr).
  Flight bookings keep the old rule.
- booking_status gains:
  hold_pending     our record is written before the supplier is called;
  hold_failed      the supplier didn't hold (or didn't confirm): nothing is pending;
  hold_unrecorded  the supplier held but the booking couldn't be completed; the supplier PNR is kept.
"""

from alembic import op

revision = "0020_group_hold_states"
down_revision = "0019_payment_accounts"
branch_labels = None
depends_on = None

OLD_STATUSES = "'held', 'pending_payment', 'payment_received', 'confirmed', 'ticket_issued', 'cancelled', 'expired'"
NEW_STATUSES = OLD_STATUSES + ", 'hold_pending', 'hold_failed', 'hold_unrecorded'"


def upgrade():
    op.drop_constraint("ck_bookings_pnr", "bookings", type_="check")
    op.execute("ALTER TABLE bookings ALTER COLUMN pnr TYPE varchar(64)")
    op.create_check_constraint(
        "ck_bookings_pnr", "bookings",
        "pnr IS NULL OR pnr ~ '^[A-Z0-9]{1,16}$' OR (product = 'group' AND pnr ~ '^[A-Za-z0-9][A-Za-z0-9 /-]{0,63}$')",
    )
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", f"booking_status IN ({NEW_STATUSES})")


def downgrade():
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", f"booking_status IN ({OLD_STATUSES})")
    op.drop_constraint("ck_bookings_pnr", "bookings", type_="check")
    op.execute("ALTER TABLE bookings ALTER COLUMN pnr TYPE varchar(16)")
    op.create_check_constraint("ck_bookings_pnr", "bookings", "pnr IS NULL OR pnr ~ '^[A-Z0-9]{1,16}$'")
