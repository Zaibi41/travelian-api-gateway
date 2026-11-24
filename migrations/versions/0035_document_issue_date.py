"""Issue date on a saved document (travelian#12, default applied: an optional issue date next to the expiry).

Additive only: saved_person_documents gains document_issue_date. Nullable: documents saved before this show no issue
date, and booking forms don't ask for it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_document_issue_date"
down_revision = "0034_payment_account_logo"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("saved_person_documents", sa.Column("document_issue_date", sa.Date(), nullable=True))


def downgrade():
    op.drop_column("saved_person_documents", "document_issue_date")
