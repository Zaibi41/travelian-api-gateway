"""Business contacts; existing records retain null contact fields."""

import sqlalchemy as sa
from alembic import op

revision = "0005_agency_contacts"
down_revision = "0004_agency_integrations"
branch_labels = None
depends_on = None


def upgrade():
    for name, length in [
        ("contact_name", 200),
        ("contact_email", 254),
        ("contact_phone", 16),
    ]:
        op.add_column("agencies", sa.Column(name, sa.String(length), nullable=True))
    op.create_check_constraint(
        "ck_agency_contact_name",
        "agencies",
        "contact_name IS NULL OR length(trim(contact_name)) > 0",
    )
    op.create_check_constraint(
        "ck_agency_contact_email",
        "agencies",
        "contact_email IS NULL OR length(trim(contact_email)) > 0",
    )
    op.create_check_constraint(
        "ck_agency_contact_phone",
        "agencies",
        r"contact_phone IS NULL OR contact_phone ~ '^\+[1-9][0-9]{1,14}$'",
    )


def downgrade():
    for name in [
        "ck_agency_contact_name",
        "ck_agency_contact_email",
        "ck_agency_contact_phone",
    ]:
        op.drop_constraint(name, "agencies", type_="check")
    for name in ["contact_phone", "contact_email", "contact_name"]:
        op.drop_column("agencies", name)
