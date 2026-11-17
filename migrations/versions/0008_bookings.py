"""Gateway-owned bookings (Admin Dashboard source of truth).

Replaces the legacy DynamoDB content bookings as the dashboard source.
Money is integer minor units (paisa for PKR). `import_source` marks rows loaded
by a one-time import (e.g. legacy test data) so they can be found and removed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_bookings"
down_revision = "0007_user_administration"
branch_labels = None
depends_on = None

PRODUCTS = ("flight", "group", "hotel", "umrah")
BOOKING_STATUSES = ("held", "pending_payment", "payment_received", "confirmed", "ticket_issued", "cancelled", "expired")


def _in(column, values):
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade():
    op.create_table(
        "bookings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("reference", sa.String(64), nullable=False, unique=True),
        sa.Column("pnr", sa.String(16), nullable=True),
        sa.Column("product", sa.String(16), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=True),
        sa.Column("agency_id", sa.String(128), sa.ForeignKey("agencies.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("customer_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("booking_status", sa.String(24), nullable=False),
        sa.Column("payment_status", sa.String(8), nullable=False, server_default="unpaid"),
        sa.Column("issuance_status", sa.String(16), nullable=False, server_default="not_issued"),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.Column("supplier_cost_minor", sa.BigInteger(), nullable=True),
        sa.Column("import_source", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(_in("product", PRODUCTS), name="ck_bookings_product"),
        sa.CheckConstraint(_in("booking_status", BOOKING_STATUSES), name="ck_bookings_status"),
        sa.CheckConstraint(_in("payment_status", ("paid", "unpaid")), name="ck_bookings_payment"),
        sa.CheckConstraint(_in("issuance_status", ("not_issued", "issued")), name="ck_bookings_issuance"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_bookings_currency"),
        sa.CheckConstraint("total_minor >= 0", name="ck_bookings_total"),
        sa.CheckConstraint("supplier_cost_minor IS NULL OR supplier_cost_minor >= 0", name="ck_bookings_cost"),
        sa.CheckConstraint("pnr IS NULL OR pnr ~ '^[A-Z0-9]{1,16}$'", name="ck_bookings_pnr"),
    )
    op.create_index("ix_bookings_created_at", "bookings", ["created_at"])
    op.create_index("ix_bookings_product_provider", "bookings", ["product", "provider_id"])
    op.create_index("ix_bookings_agency", "bookings", ["agency_id"])
    op.create_index(
        "ix_bookings_open_holds", "bookings", ["hold_expires_at"],
        postgresql_where=sa.text("booking_status IN ('held', 'pending_payment')"),
    )


def downgrade():
    op.drop_table("bookings")
