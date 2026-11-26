"""Ticket Sale Entry corrections and deletes (Super Admin only). Additive.

No row is ever removed. A correction to a confirmed entry's details only (passenger, channel, travel date, route, PNR, notes) is
saved in place and logged in finance_audit_log with the reason. A correction to anything that moves money (cost, sale price,
amount received, airline ID, payment account, ticket status) marks the original "reversed" (so it drops out of every balance)
and saves a new confirmed entry that points back at it (corrects_sale_id), in one transaction.
A pending entry (nothing posted yet) is corrected in place.

A delete never removes the row: it is marked "deleted" (out of the list and every balance), with who, when and why, and the
audit log keeps the full entry as it was. reversed_at / reversed_by_user_id / correction_reason record both.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040_ticket_sale_corrections"
down_revision = "0039_booking_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket_sale_entries", sa.Column("corrects_sale_id", sa.String(36),
                                                   sa.ForeignKey("ticket_sale_entries.id", ondelete="RESTRICT"), nullable=True))
    op.add_column("ticket_sale_entries", sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ticket_sale_entries", sa.Column("reversed_by_user_id", sa.String(36),
                                                   sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True))
    op.add_column("ticket_sale_entries", sa.Column("correction_reason", sa.Text(), nullable=True))
    op.drop_constraint("ck_tse_state", "ticket_sale_entries", type_="check")
    op.create_check_constraint("ck_tse_state", "ticket_sale_entries", "state IN ('confirmed', 'pending', 'rejected', 'reversed', 'deleted')")
    op.create_check_constraint("ck_tse_reversed", "ticket_sale_entries",
                               "(state <> 'reversed' OR confirmed_at IS NOT NULL)"
                               " AND (state NOT IN ('reversed', 'deleted') OR (reversed_at IS NOT NULL AND correction_reason IS NOT NULL))")
    op.create_index("ux_tse_corrects", "ticket_sale_entries", ["corrects_sale_id"], unique=True,
                    postgresql_where=sa.text("corrects_sale_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ux_tse_corrects", table_name="ticket_sale_entries")
    op.drop_constraint("ck_tse_reversed", "ticket_sale_entries", type_="check")
    op.drop_constraint("ck_tse_state", "ticket_sale_entries", type_="check")
    op.create_check_constraint("ck_tse_state", "ticket_sale_entries", "state IN ('confirmed', 'pending', 'rejected')")
    op.drop_column("ticket_sale_entries", "correction_reason")
    op.drop_column("ticket_sale_entries", "reversed_by_user_id")
    op.drop_column("ticket_sale_entries", "reversed_at")
    op.drop_column("ticket_sale_entries", "corrects_sale_id")
