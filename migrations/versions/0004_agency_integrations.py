"""Agency machine client registry; secrets remain with Cognito."""

import sqlalchemy as sa
from alembic import op

revision = "0004_agency_integrations"
down_revision = "0003_agency_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agency_integrations",
        sa.Column("issuer", sa.String(255), primary_key=True),
        sa.Column("client_id", sa.String(128), primary_key=True),
        sa.Column(
            "agency_id",
            sa.String(128),
            sa.ForeignKey("agencies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("scopes", sa.ARRAY(sa.String(256)), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_table("agency_integrations")
