"""Agency identities with one Cognito account per agency.

Revision ID: 0001_agency_identity
Revises: none
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_agency_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agencies",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="suspended"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'", name="ck_agencies_id"
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_agencies_name"),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name="ck_agencies_status"
        ),
    )
    op.create_table(
        "agency_cognito_assignments",
        sa.Column(
            "agency_id",
            sa.String(128),
            sa.ForeignKey("agencies.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("issuer", "subject", name="uq_agency_cognito_identity"),
        sa.CheckConstraint(
            "length(trim(issuer)) > 0 AND issuer = trim(issuer)",
            name="ck_assignment_issuer",
        ),
        sa.CheckConstraint(
            "length(trim(subject)) > 0 AND subject = trim(subject)",
            name="ck_assignment_subject",
        ),
    )


def downgrade() -> None:
    op.drop_table("agency_cognito_assignments")
    op.drop_table("agencies")
