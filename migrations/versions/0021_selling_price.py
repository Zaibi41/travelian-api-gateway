"""Supplier cost and selling price per traveler and per booking (Group Ticketing and Flights, one model).

- bookings.total_minor stays the selling total (what the customer pays; payments are capped at it).
- bookings.supplier_cost_minor is the cost total. Existing bookings without a cost get cost = their current total
  (profit 0); bookings that already have a recorded cost keep it. Then the column becomes required.
- booking_travelers.selling_minor and cost_minor: each traveler's share. Existing travelers get their booking's
  totals split evenly (any remainder on the first traveler). Added nullable, backfilled, then made required.
Nothing changes any total, payment, received or due amount.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_selling_price"
down_revision = "0020_group_hold_states"
branch_labels = None
depends_on = None

SPLIT = """
WITH counted AS (
    SELECT t.id, t.position, b.total_minor AS selling, b.supplier_cost_minor AS cost,
           count(*) OVER (PARTITION BY t.booking_id) AS n,
           row_number() OVER (PARTITION BY t.booking_id ORDER BY t.position) AS rn
    FROM booking_travelers t JOIN bookings b ON b.id = t.booking_id
)
UPDATE booking_travelers t
SET selling_minor = c.selling / c.n + CASE WHEN c.rn = 1 THEN c.selling % c.n ELSE 0 END,
    cost_minor = c.cost / c.n + CASE WHEN c.rn = 1 THEN c.cost % c.n ELSE 0 END
FROM counted c
WHERE t.id = c.id AND (t.selling_minor IS NULL OR t.cost_minor IS NULL)
"""


def upgrade():
    op.execute("UPDATE bookings SET supplier_cost_minor = total_minor WHERE supplier_cost_minor IS NULL")
    op.alter_column("bookings", "supplier_cost_minor", nullable=False)
    op.add_column("booking_travelers", sa.Column("selling_minor", sa.BigInteger(), nullable=True))
    op.add_column("booking_travelers", sa.Column("cost_minor", sa.BigInteger(), nullable=True))
    op.execute(SPLIT)
    op.alter_column("booking_travelers", "selling_minor", nullable=False)
    op.alter_column("booking_travelers", "cost_minor", nullable=False)
    op.create_check_constraint("ck_booking_travelers_amounts", "booking_travelers", "selling_minor >= 0 AND cost_minor >= 0")


def downgrade():
    op.drop_constraint("ck_booking_travelers_amounts", "booking_travelers", type_="check")
    op.drop_column("booking_travelers", "cost_minor")
    op.drop_column("booking_travelers", "selling_minor")
    op.alter_column("bookings", "supplier_cost_minor", nullable=True)
