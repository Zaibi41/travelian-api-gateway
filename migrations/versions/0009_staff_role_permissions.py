"""Staff role and grantable field-visibility permissions (spec v3.0 D23).

- Role `staff` (id 4).
- role_permissions / user_permissions: grant "see_supplier_names" or
  "see_cost_and_margin" per role or per user. Grants only take effect for the
  staff role (enforced in app.core.field_visibility); super_admin always sees
  these fields, agency and customer roles never do and cannot be granted them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_staff_role_permissions"
down_revision = "0008_bookings"
branch_labels = None
depends_on = None

PERMISSIONS = ("see_supplier_names", "see_cost_and_margin")
CHECK = "permission IN ('see_supplier_names', 'see_cost_and_margin')"


def upgrade():
    op.execute("INSERT INTO roles (id, name) VALUES (4, 'staff') ON CONFLICT (id) DO NOTHING")
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.SmallInteger(), sa.ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("permission", sa.String(48), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(CHECK, name="ck_role_permissions_permission"),
    )
    op.create_table(
        "user_permissions",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission", sa.String(48), primary_key=True),
        sa.Column("granted_by", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(CHECK, name="ck_user_permissions_permission"),
    )


def downgrade():
    op.drop_table("user_permissions")
    op.drop_table("role_permissions")
    op.execute("DELETE FROM roles WHERE id = 4 AND NOT EXISTS (SELECT 1 FROM users WHERE role_id = 4)")
