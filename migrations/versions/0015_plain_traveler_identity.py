"""Traveler identity as plain text (sandbox phase); drop encryption columns.

booking_travelers: date_of_birth (date) and document_number replace the encrypted values, their
stored masked copies, the keyed document hash and the key id. Masking now happens on read, and
the full values still come only from the logged reveal (docs/pii-storage.md). Protecting these
values in the database is a go-live blocker.

Existing traveler rows were sandbox test data encrypted with keys that are no longer kept, so
they can't be converted: they are deleted. Their bookings, customers, payments and audit events
are kept (the Passengers tab then shows "not available" for those bookings).

Numbering: role alignment moves to 0016.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_plain_traveler_identity"
down_revision = "0014_issuance"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DELETE FROM booking_travelers")
    op.drop_index("ix_booking_travelers_document_hash", table_name="booking_travelers")
    op.drop_index("ix_booking_travelers_key_id", table_name="booking_travelers")
    for column in ("date_of_birth_encrypted", "date_of_birth_masked", "document_number_encrypted", "document_number_masked",
                   "document_number_hash", "key_id"):
        op.drop_column("booking_travelers", column)
    op.add_column("booking_travelers", sa.Column("date_of_birth", sa.Date(), nullable=False))
    op.add_column("booking_travelers", sa.Column("document_number", sa.String(32), nullable=True))


def downgrade():
    op.execute("DELETE FROM booking_travelers")
    op.drop_column("booking_travelers", "document_number")
    op.drop_column("booking_travelers", "date_of_birth")
    op.add_column("booking_travelers", sa.Column("date_of_birth_encrypted", sa.Text(), nullable=False))
    op.add_column("booking_travelers", sa.Column("date_of_birth_masked", sa.String(16), nullable=False))
    op.add_column("booking_travelers", sa.Column("document_number_encrypted", sa.Text(), nullable=True))
    op.add_column("booking_travelers", sa.Column("document_number_masked", sa.String(32), nullable=True))
    op.add_column("booking_travelers", sa.Column("document_number_hash", sa.String(64), nullable=True))
    op.add_column("booking_travelers", sa.Column("key_id", sa.String(32), nullable=False))
    op.create_index("ix_booking_travelers_key_id", "booking_travelers", ["key_id"])
    op.create_index("ix_booking_travelers_document_hash", "booking_travelers", ["document_number_hash"])
