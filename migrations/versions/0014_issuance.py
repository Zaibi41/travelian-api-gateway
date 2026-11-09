"""Ticket issuance workflow: requested / processing statuses, ticket numbers, issue_tickets permission.

- bookings.issuance_status: not_issued, requested (awaiting an approver), processing (a ticketing
  call is in progress; blocks a second one), issued, unknown (supplier outcome unclear; blocks
  issuing until resolved with a reason).
- booking_travelers.ticket_number per traveler.
- Permission "issue_tickets" (grantable to staff; super_admin always): issue, verify with the
  supplier, and resolve an unknown issuance.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_issuance"
down_revision = "0013_booking_payments"
branch_labels = None
depends_on = None

OLD_PERMISSIONS = "permission IN ('see_supplier_names', 'see_cost_and_margin', 'reveal_identity', 'record_payments')"
NEW_PERMISSIONS = "permission IN ('see_supplier_names', 'see_cost_and_margin', 'reveal_identity', 'record_payments', 'issue_tickets')"


def _permissions(check: str) -> None:
    for table in ("role_permissions", "user_permissions"):
        op.drop_constraint(f"ck_{table}_permission", table, type_="check")
        op.create_check_constraint(f"ck_{table}_permission", table, check)


def upgrade():
    op.drop_constraint("ck_bookings_issuance", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_issuance", "bookings",
                               "issuance_status IN ('not_issued', 'requested', 'processing', 'issued', 'unknown')")
    op.add_column("booking_travelers", sa.Column("ticket_number", sa.String(32), nullable=True))
    _permissions(NEW_PERMISSIONS)


def downgrade():
    for table in ("role_permissions", "user_permissions"):
        op.execute(f"DELETE FROM {table} WHERE permission = 'issue_tickets'")
    _permissions(OLD_PERMISSIONS)
    op.drop_column("booking_travelers", "ticket_number")
    op.drop_constraint("ck_bookings_issuance", "bookings", type_="check")
    op.execute("UPDATE bookings SET issuance_status = 'unknown' WHERE issuance_status = 'processing'")
    op.execute("UPDATE bookings SET issuance_status = 'not_issued' WHERE issuance_status = 'requested'")
    op.create_check_constraint("ck_bookings_issuance", "bookings", "issuance_status IN ('not_issued', 'issued', 'unknown')")
