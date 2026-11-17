"""Customer and travelers of a booking, with identity data encrypted; reveal_identity permission.

- booking_customers: who pays and is contacted (one per booking): names, phone, email.
- booking_travelers: who flies, one row per passenger in booking order. Date of birth and
  document number are stored ENCRYPTED (AES-256-GCM, app/core/pii.py) with the key id in the
  value, plus a masked copy for display and a keyed hash of the document for duplicate checks.
  Names, title, type, ID type, document country, nationality and expiry are plain.
- Permission "reveal_identity" (spec 3.2): staff may be granted it; every reveal is logged in
  booking_events. Migration 0012 is reserved for role alignment (admin, sub_agency).
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_booking_people"
down_revision = "0010_supplier_holds"
branch_labels = None
depends_on = None

OLD_CHECK = "permission IN ('see_supplier_names', 'see_cost_and_margin')"
NEW_CHECK = "permission IN ('see_supplier_names', 'see_cost_and_margin', 'reveal_identity')"


def upgrade():
    op.create_table(
        "booking_customers",
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("first_name", sa.String(64), nullable=False),
        sa.Column("last_name", sa.String(64), nullable=False),
        sa.Column("phone_code", sa.String(4), nullable=False),
        sa.Column("phone", sa.String(16), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "booking_travelers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("passenger_type", sa.String(3), nullable=False),
        sa.Column("title", sa.String(8), nullable=False),
        sa.Column("first_name", sa.String(64), nullable=False),
        sa.Column("last_name", sa.String(64), nullable=False),
        sa.Column("date_of_birth_encrypted", sa.Text(), nullable=False),
        sa.Column("date_of_birth_masked", sa.String(16), nullable=False),
        sa.Column("id_type", sa.String(8), nullable=False),
        sa.Column("document_number_encrypted", sa.Text(), nullable=True),
        sa.Column("document_number_masked", sa.String(32), nullable=True),
        sa.Column("document_number_hash", sa.String(64), nullable=True),
        sa.Column("document_country", sa.String(3), nullable=True),
        sa.Column("nationality", sa.String(3), nullable=True),
        sa.Column("document_expiry", sa.Date(), nullable=True),
        sa.Column("infant_of_position", sa.SmallInteger(), nullable=True),
        sa.Column("key_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("booking_id", "position", name="uq_booking_travelers_position"),
        sa.CheckConstraint("passenger_type IN ('ADT', 'CHD', 'INF')", name="ck_booking_travelers_type"),
        sa.CheckConstraint("id_type IN ('PSPT', 'NATI')", name="ck_booking_travelers_id_type"),
    )
    op.create_index("ix_booking_travelers_document_hash", "booking_travelers", ["document_number_hash"])
    op.create_index("ix_booking_travelers_key_id", "booking_travelers", ["key_id"])
    for table in ("role_permissions", "user_permissions"):
        op.drop_constraint(f"ck_{table}_permission", table, type_="check")
        op.create_check_constraint(f"ck_{table}_permission", table, NEW_CHECK)


def downgrade():
    for table in ("role_permissions", "user_permissions"):
        op.execute(f"DELETE FROM {table} WHERE permission = 'reveal_identity'")
        op.drop_constraint(f"ck_{table}_permission", table, type_="check")
        op.create_check_constraint(f"ck_{table}_permission", table, OLD_CHECK)
    op.drop_index("ix_booking_travelers_key_id", table_name="booking_travelers")
    op.drop_index("ix_booking_travelers_document_hash", table_name="booking_travelers")
    op.drop_table("booking_travelers")
    op.drop_table("booking_customers")
