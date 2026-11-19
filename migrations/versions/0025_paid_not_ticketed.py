"""'paid_not_ticketed': a flight the customer paid for that can't be ticketed (the hold expired, the fare is gone or the
supplier refuses). Staff refund or rebook by hand; nothing is refunded automatically. The reason is kept in
bookings.supplier_response -> 'action_needed'.
"""

from alembic import op

revision = "0025_paid_not_ticketed"
down_revision = "0024_receiving_account_numbers"
branch_labels = None
depends_on = None

STATUSES = ("'held', 'pending_payment', 'payment_received', 'confirmed', 'ticket_issued', 'cancelled', 'expired',"
            " 'hold_pending', 'hold_failed', 'hold_unrecorded', 'requested'")


def upgrade() -> None:
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", f"booking_status IN ({STATUSES}, 'paid_not_ticketed')")


def downgrade() -> None:
    # Bookings in the new state go back to 'expired' (the state they had before it existed).
    op.execute("UPDATE bookings SET booking_status = 'expired' WHERE booking_status = 'paid_not_ticketed'")
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", f"booking_status IN ({STATUSES})")
