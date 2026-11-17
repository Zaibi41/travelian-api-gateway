"""Pending agency applications; no approval or booking entitlement.

Revision ID: 0002_agency_applications
Revises: 0001_agency_identity
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_agency_applications"
down_revision = "0001_agency_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agency_applications",
        sa.Column(
            "agency_id",
            sa.String(128),
            sa.ForeignKey("agency_cognito_assignments.agency_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="pending_review"
        ),
        sa.Column(
            "submitted_at",
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
            "status IN ('pending_review', 'more_information_required', 'approved', 'rejected')",
            name="ck_agency_application_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("agency_applications")
