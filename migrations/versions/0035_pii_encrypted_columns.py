"""Room for encrypted identity values (gateway#84). Additive only; runs before the code that uses it.

- booking_travelers: date_of_birth_enc, document_number_enc (AES-256-GCM ciphertext, "<key id>:<base64>").
- saved_people: date_of_birth_enc and date_of_birth_hmac (keyed hash: same name + date of birth matching without
  the plain date).
- saved_person_documents: document_number_enc and document_number_hmac (keyed hash of the normalised number: the
  document match key and exact document search keep working); a unique index on the hashed key like the plain one.
- The plain columns become nullable: once a value is encrypted its plain copy is cleared. Check constraints keep
  every row holding the value one way or the other. Nothing is dropped and no row changes here: until the key is
  configured and the backfill (python -m app.cli.encrypt_identity) is run, the gateway keeps reading and writing the
  plain columns exactly as before.

Downgrade restores NOT NULL, so it needs every value back in plain text first (python -m app.cli.encrypt_identity
--decrypt --apply).
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_pii_encrypted_columns"
down_revision = "0034_payment_account_logo"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("booking_travelers", sa.Column("date_of_birth_enc", sa.Text(), nullable=True))
    op.add_column("booking_travelers", sa.Column("document_number_enc", sa.Text(), nullable=True))
    op.alter_column("booking_travelers", "date_of_birth", existing_type=sa.Date(), nullable=True)
    op.create_check_constraint("ck_booking_travelers_date_of_birth_stored", "booking_travelers",
                               "date_of_birth IS NOT NULL OR date_of_birth_enc IS NOT NULL")

    op.add_column("saved_people", sa.Column("date_of_birth_enc", sa.Text(), nullable=True))
    op.add_column("saved_people", sa.Column("date_of_birth_hmac", sa.String(64), nullable=True))
    op.create_index("ix_saved_people_date_of_birth_hmac", "saved_people", ["date_of_birth_hmac"])

    op.add_column("saved_person_documents", sa.Column("document_number_enc", sa.Text(), nullable=True))
    op.add_column("saved_person_documents", sa.Column("document_number_hmac", sa.String(64), nullable=True))
    op.alter_column("saved_person_documents", "document_number", existing_type=sa.String(32), nullable=True)
    op.alter_column("saved_person_documents", "document_number_normalized", existing_type=sa.String(32), nullable=True)
    op.create_check_constraint("ck_saved_person_documents_number_stored", "saved_person_documents",
                               "document_number IS NOT NULL OR document_number_enc IS NOT NULL")
    op.create_check_constraint("ck_saved_person_documents_key_stored", "saved_person_documents",
                               "document_number_normalized IS NOT NULL OR document_number_hmac IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX uq_saved_person_documents_hmac_key ON saved_person_documents"
               " (coalesce(owner_agency_id, ''), id_type, document_country, document_number_hmac)"
               " WHERE removed_at IS NULL AND document_number_hmac IS NOT NULL")


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_saved_person_documents_hmac_key")
    op.drop_constraint("ck_saved_person_documents_key_stored", "saved_person_documents", type_="check")
    op.drop_constraint("ck_saved_person_documents_number_stored", "saved_person_documents", type_="check")
    op.alter_column("saved_person_documents", "document_number_normalized", existing_type=sa.String(32), nullable=False)
    op.alter_column("saved_person_documents", "document_number", existing_type=sa.String(32), nullable=False)
    op.drop_column("saved_person_documents", "document_number_hmac")
    op.drop_column("saved_person_documents", "document_number_enc")
    op.drop_index("ix_saved_people_date_of_birth_hmac", table_name="saved_people")
    op.drop_column("saved_people", "date_of_birth_hmac")
    op.drop_column("saved_people", "date_of_birth_enc")
    op.drop_constraint("ck_booking_travelers_date_of_birth_stored", "booking_travelers", type_="check")
    op.alter_column("booking_travelers", "date_of_birth", existing_type=sa.Date(), nullable=False)
    op.drop_column("booking_travelers", "document_number_enc")
    op.drop_column("booking_travelers", "date_of_birth_enc")
