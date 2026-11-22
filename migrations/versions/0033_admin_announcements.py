"""Admin announcements (travelian#92): short messages a Super Admin posts for every staff member's admin top bar.

Additive only: two new tables, nothing existing changes.

- admin_announcements: the message (plain text, 2 to 240 characters), who posted it and when, an optional start and
  end time, and an active flag. Ending one sets active = false with who/when; deleting one hides it everywhere
  (deleted_at, who) but keeps the row, so the audit trail stays complete.
- admin_announcement_events: the audit log, one row per create / end / delete (who, their role, when). Rows are never
  changed or deleted (trigger).
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_admin_announcements"
down_revision = "0032_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_announcements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.CheckConstraint("char_length(message) BETWEEN 2 AND 240", name="ck_admin_announcements_message_length"),
        sa.CheckConstraint("ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at", name="ck_admin_announcements_window"),
        sa.CheckConstraint("active OR ended_at IS NOT NULL", name="ck_admin_announcements_ended"),
    )
    op.create_index("ix_admin_announcements_live", "admin_announcements", ["created_at"],
                    postgresql_where=sa.text("active AND deleted_at IS NULL"))
    op.create_table(
        "admin_announcement_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("announcement_id", sa.String(36), sa.ForeignKey("admin_announcements.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("actor_role", sa.String(32), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("action IN ('create', 'end', 'delete')", name="ck_admin_announcement_events_action"),
    )
    op.create_index("ix_admin_announcement_events_announcement", "admin_announcement_events", ["announcement_id", "id"])
    op.execute("""
        CREATE FUNCTION admin_announcement_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'admin_announcement_events rows are never changed or deleted';
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER admin_announcement_events_append_only BEFORE UPDATE OR DELETE ON admin_announcement_events
        FOR EACH ROW EXECUTE FUNCTION admin_announcement_events_append_only();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS admin_announcement_events_append_only ON admin_announcement_events")
    op.execute("DROP FUNCTION IF EXISTS admin_announcement_events_append_only()")
    op.drop_index("ix_admin_announcement_events_announcement", table_name="admin_announcement_events")
    op.drop_table("admin_announcement_events")
    op.drop_index("ix_admin_announcements_live", table_name="admin_announcements")
    op.drop_table("admin_announcements")
