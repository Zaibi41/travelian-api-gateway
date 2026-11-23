"""Bank logo per receiving account (travelian#76, default applied: option 2, a logo upload per account).

Additive only: payment_accounts gains logo_asset_key, the S3 object key of the bank's logo image (the file itself lives
in S3, like the QR image). Nullable: until a logo is uploaded, checkout keeps the bank's initials.
"""

import sqlalchemy as sa
from alembic import op

revision = "0034_payment_account_logo"
down_revision = "0033_admin_announcements"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payment_accounts", sa.Column("logo_asset_key", sa.String(300), nullable=True))


def downgrade():
    op.drop_column("payment_accounts", "logo_asset_key")
