"""Revision-protected administrative user changes and audit history."""

import sqlalchemy as sa
from alembic import op

revision = "0007_user_administration"
down_revision = "0006_users_roles"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_table(
        "user_admin_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("previous_role_id", sa.SmallInteger(), nullable=False),
        sa.Column("new_role_id", sa.SmallInteger(), nullable=False),
        sa.Column("previous_active", sa.Boolean(), nullable=False),
        sa.Column("new_active", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "revision", name="uq_user_admin_revision"),
    )


def downgrade():
    op.drop_table("user_admin_events")
    op.drop_column("users", "revision")
