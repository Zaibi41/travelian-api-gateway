"""Staff notifications, bank-transfer account details, uploaded assets.

- admin_notifications: one row per event staff must act on (hold created, booking request, restricted-airline request,
  bank-transfer hold). Shown in the admin; also emailed when email is configured. No passenger documents.
- payment_accounts gains bank_name, account_title, account_number_masked (never the full number) and qr_asset_key
  (the S3 object key of the QR image; the file itself lives in S3).
- site_assets: admin-uploaded media by slot (e.g. 'site_logo'): the S3 key and its public URL. Never the file.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_notifications_and_accounts"
down_revision = "0022_public_booking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("booking_id", sa.String(36), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reference", sa.String(64), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("emailed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_admin_notifications_created", "admin_notifications", ["created_at"])
    op.add_column("payment_accounts", sa.Column("bank_name", sa.String(120), nullable=True))
    op.add_column("payment_accounts", sa.Column("account_title", sa.String(120), nullable=True))
    op.add_column("payment_accounts", sa.Column("account_number_masked", sa.String(40), nullable=True))
    op.add_column("payment_accounts", sa.Column("qr_asset_key", sa.String(300), nullable=True))
    op.create_check_constraint("ck_payment_accounts_masked", "payment_accounts",
                               "account_number_masked IS NULL OR account_number_masked ~ '^[*Xx• 0-9-]{4,40}$'")
    op.create_table(
        "site_assets",
        sa.Column("slot", sa.String(40), primary_key=True),
        sa.Column("asset_key", sa.String(300), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("updated_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("site_assets")
    op.drop_constraint("ck_payment_accounts_masked", "payment_accounts", type_="check")
    for column in ("qr_asset_key", "account_number_masked", "account_title", "bank_name"):
        op.drop_column("payment_accounts", column)
    op.drop_index("ix_admin_notifications_created", table_name="admin_notifications")
    op.drop_table("admin_notifications")
