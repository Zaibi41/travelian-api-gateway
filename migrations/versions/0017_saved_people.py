"""Saved people (customers and travelers) with their documents; links from booking copies.

- saved_people: one record per real person, owned by Travelian (owner_agency_id NULL) or by one
  agency. A person can be a customer, a traveler, or both. Identity is plain text in the sandbox
  phase (docs/pii-storage.md). merged_into_id is set only by a deliberate, logged merge.
- saved_person_documents: a person's documents (CNIC, passport...). The match key is owner +
  id_type + document country + normalised number, unique among documents not removed.
- saved_person_events: audit of create, edit, documents, duplicate choices, merges, reveals
  (ids and field names only).
- booking_customers.person_id, booking_travelers.person_id / document_id: nullable links from
  each booking's own copy to the saved person. Past booking copies never change.
- Search indexes: document key, normalised email and phone, names (prefix); the missing indexes
  on booking_customers (person, email).
- Backfill: one saved customer per (owner, normalised email) from existing booking_customers,
  linked to those rows. Idempotent; no travelers, no documents, no merging.
- Permissions "manage_people" (create, edit, documents) and "merge_people" (grantable to staff;
  super_admin always).

Numbering: 0016 stays reserved for role alignment.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_saved_people"
down_revision = "0015_plain_traveler_identity"
branch_labels = None
depends_on = None

OLD_PERMISSIONS = "permission IN ('see_supplier_names', 'see_cost_and_margin', 'reveal_identity', 'record_payments', 'issue_tickets')"
NEW_PERMISSIONS = ("permission IN ('see_supplier_names', 'see_cost_and_margin', 'reveal_identity', 'record_payments', 'issue_tickets',"
                   " 'manage_people', 'merge_people')")

# Frozen copy of app.db.saved_people.BACKFILL_SQL (a migration must not depend on later code).
BACKFILL_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (b.agency_id, lower(trim(c.email)))
           b.agency_id AS owner, lower(trim(c.email)) AS email_normalized, c.first_name, c.last_name, c.phone_code, c.phone, c.email
      FROM booking_customers c JOIN bookings b ON b.id = c.booking_id
     WHERE c.person_id IS NULL AND trim(coalesce(c.email, '')) <> ''
     ORDER BY b.agency_id, lower(trim(c.email)), c.created_at DESC
)
INSERT INTO saved_people (id, owner_agency_id, first_name, last_name, phone_code, phone, phone_normalized, email, email_normalized)
SELECT gen_random_uuid()::text, l.owner, l.first_name, l.last_name, l.phone_code, l.phone,
       nullif(regexp_replace(coalesce(l.phone_code, '') || regexp_replace(coalesce(l.phone, ''), '^0+', ''), '[^0-9]', '', 'g'), ''),
       l.email, l.email_normalized
  FROM latest l
 WHERE NOT EXISTS (SELECT 1 FROM saved_people p WHERE p.email_normalized = l.email_normalized
                     AND p.owner_agency_id IS NOT DISTINCT FROM l.owner AND p.merged_into_id IS NULL);
UPDATE booking_customers c SET person_id = p.id
  FROM bookings b, saved_people p
 WHERE b.id = c.booking_id AND c.person_id IS NULL AND trim(coalesce(c.email, '')) <> ''
   AND p.email_normalized = lower(trim(c.email)) AND p.owner_agency_id IS NOT DISTINCT FROM b.agency_id AND p.merged_into_id IS NULL;
"""


def _permissions(check: str) -> None:
    for table in ("role_permissions", "user_permissions"):
        op.drop_constraint(f"ck_{table}_permission", table, type_="check")
        op.create_check_constraint(f"ck_{table}_permission", table, check)


def upgrade():
    op.create_table(
        "saved_people",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_agency_id", sa.String(128), sa.ForeignKey("agencies.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("title", sa.String(8), nullable=True),
        sa.Column("first_name", sa.String(64), nullable=False),
        sa.Column("last_name", sa.String(64), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("nationality", sa.String(3), nullable=True),
        sa.Column("phone_code", sa.String(4), nullable=True),
        sa.Column("phone", sa.String(16), nullable=True),
        sa.Column("phone_normalized", sa.String(24), nullable=True),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("email_normalized", sa.String(254), nullable=True),
        sa.Column("merged_into_id", sa.String(36), sa.ForeignKey("saved_people.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("title IS NULL OR title IN ('MR', 'MRS', 'MISS', 'MASTER')", name="ck_saved_people_title"),
    )
    op.create_index("ix_saved_people_email", "saved_people", ["owner_agency_id", "email_normalized"])
    op.create_index("ix_saved_people_phone", "saved_people", ["owner_agency_id", "phone_normalized"])
    op.execute("CREATE INDEX ix_saved_people_last_name ON saved_people (owner_agency_id, lower(last_name) text_pattern_ops)")
    op.execute("CREATE INDEX ix_saved_people_first_name ON saved_people (owner_agency_id, lower(first_name) text_pattern_ops)")
    op.create_table(
        "saved_person_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("saved_people.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("owner_agency_id", sa.String(128), sa.ForeignKey("agencies.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("id_type", sa.String(8), nullable=False),
        sa.Column("document_country", sa.String(3), nullable=False),
        sa.Column("document_number", sa.String(32), nullable=False),
        sa.Column("document_number_normalized", sa.String(32), nullable=False),
        sa.Column("document_expiry", sa.Date(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id_type IN ('PSPT', 'NATI')", name="ck_saved_person_documents_id_type"),
    )
    op.execute("CREATE UNIQUE INDEX uq_saved_person_documents_key ON saved_person_documents"
               " (coalesce(owner_agency_id, ''), id_type, document_country, document_number_normalized) WHERE removed_at IS NULL")
    op.create_index("ix_saved_person_documents_person", "saved_person_documents", ["person_id"])
    op.create_table(
        "saved_person_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("saved_people.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
    )
    op.create_index("ix_saved_person_events_person", "saved_person_events", ["person_id", "created_at"])
    op.add_column("booking_customers", sa.Column("person_id", sa.String(36), sa.ForeignKey("saved_people.id", ondelete="SET NULL"), nullable=True))
    op.add_column("booking_travelers", sa.Column("person_id", sa.String(36), sa.ForeignKey("saved_people.id", ondelete="SET NULL"), nullable=True))
    op.add_column("booking_travelers", sa.Column("document_id", sa.String(36), sa.ForeignKey("saved_person_documents.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_booking_customers_person", "booking_customers", ["person_id"])
    op.execute("CREATE INDEX ix_booking_customers_email ON booking_customers (lower(email))")
    op.create_index("ix_booking_travelers_person", "booking_travelers", ["person_id"])
    _permissions(NEW_PERMISSIONS)
    op.execute(BACKFILL_SQL)


def downgrade():
    for table in ("role_permissions", "user_permissions"):
        op.execute(f"DELETE FROM {table} WHERE permission IN ('manage_people', 'merge_people')")
    _permissions(OLD_PERMISSIONS)
    op.drop_index("ix_booking_travelers_person", table_name="booking_travelers")
    op.drop_index("ix_booking_customers_email", table_name="booking_customers")
    op.drop_index("ix_booking_customers_person", table_name="booking_customers")
    op.drop_column("booking_travelers", "document_id")
    op.drop_column("booking_travelers", "person_id")
    op.drop_column("booking_customers", "person_id")
    op.drop_table("saved_person_events")
    op.drop_table("saved_person_documents")
    op.drop_table("saved_people")
