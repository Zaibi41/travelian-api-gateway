"""Local user roles; no Cognito group grants are imported."""

import sqlalchemy as sa
from alembic import op

revision = "0006_users_roles"
down_revision = "0005_agency_contacts"
branch_labels = None
depends_on = None


def upgrade():
    roles = op.create_table(
        "roles",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("name", sa.String(32), nullable=False, unique=True),
    )
    op.bulk_insert(
        roles,
        [
            {"id": 1, "name": "customer"},
            {"id": 2, "name": "agency_owner"},
            {"id": 3, "name": "super_admin"},
        ],
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cognito_issuer", sa.String(255), nullable=False),
        sa.Column("cognito_sub", sa.String(128), nullable=False),
        sa.Column(
            "role_id",
            sa.SmallInteger(),
            sa.ForeignKey("roles.id", ondelete="RESTRICT"),
            nullable=False,
            server_default="1",
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.UniqueConstraint(
            "cognito_issuer", "cognito_sub", name="uq_user_cognito_identity"
        ),
        sa.UniqueConstraint(
            "id", "cognito_issuer", "cognito_sub", name="uq_user_identity_reference"
        ),
    )
    op.create_table(
        "user_role_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("previous_role_id", sa.SmallInteger(), nullable=False),
        sa.Column("new_role_id", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "agency_cognito_assignments", sa.Column("user_id", sa.String(36), nullable=True)
    )
    # Existing assigned identities are preserved. Agency owners are inferred
    # only from already approved applications, never from token groups.
    op.execute("""INSERT INTO users (id,cognito_issuer,cognito_sub,role_id)
        SELECT gen_random_uuid()::text,x.issuer,x.subject,
               CASE WHEN p.status='approved' THEN 2 ELSE 1 END
        FROM agency_cognito_assignments x LEFT JOIN agency_applications p ON p.agency_id=x.agency_id""")
    op.execute("""UPDATE agency_cognito_assignments x SET user_id=u.id FROM users u
        WHERE u.cognito_issuer=x.issuer AND u.cognito_sub=x.subject""")
    op.alter_column("agency_cognito_assignments", "user_id", nullable=False)
    op.create_unique_constraint(
        "uq_assignment_user", "agency_cognito_assignments", ["user_id"]
    )
    op.create_foreign_key(
        "fk_assignment_user_identity",
        "agency_cognito_assignments",
        "users",
        ["user_id", "issuer", "subject"],
        ["id", "cognito_issuer", "cognito_sub"],
        ondelete="RESTRICT",
    )


def downgrade():
    op.drop_constraint(
        "fk_assignment_user_identity", "agency_cognito_assignments", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_assignment_user", "agency_cognito_assignments", type_="unique"
    )
    op.drop_column("agency_cognito_assignments", "user_id")
    op.drop_table("user_role_events")
    op.drop_table("users")
    op.drop_table("roles")
