"""Agency revision and transactional review history."""

import sqlalchemy as sa
from alembic import op

revision = "0003_agency_review"
down_revision = "0002_agency_applications"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agencies",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "agency_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agency_id",
            sa.String(128),
            sa.ForeignKey("agencies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=True),
        sa.Column("actor_issuer", sa.String(255), nullable=False),
        sa.Column("actor_subject", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("agency_id", "revision", name="uq_agency_event_revision"),
    )


def downgrade():
    op.drop_table("agency_events")
    op.drop_column("agencies", "revision")
