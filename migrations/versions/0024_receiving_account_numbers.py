"""Receiving accounts keep the full account number and IBAN (D40).

The account customers pay into is Travelian's own business account, not personal data: customers need the full number
or IBAN to make a transfer, so it is stored and shown in full. Masking stays for passenger data (passport, date of
birth). account_number_masked is kept (derived from the full number) for anything that still reads it. Existing
accounts kept only the last 4 digits, so staff re-enter the number once.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_receiving_account_numbers"
down_revision = "0023_notifications_and_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payment_accounts", sa.Column("account_number", sa.String(40), nullable=True))
    op.add_column("payment_accounts", sa.Column("iban", sa.String(34), nullable=True))
    op.create_check_constraint("ck_payment_accounts_number", "payment_accounts", "account_number IS NULL OR account_number ~ '^[0-9][0-9 -]{4,38}[0-9]$'")
    op.create_check_constraint("ck_payment_accounts_iban", "payment_accounts", "iban IS NULL OR iban ~ '^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$'")


def downgrade() -> None:
    op.drop_constraint("ck_payment_accounts_iban", "payment_accounts", type_="check")
    op.drop_constraint("ck_payment_accounts_number", "payment_accounts", type_="check")
    op.drop_column("payment_accounts", "iban")
    op.drop_column("payment_accounts", "account_number")
