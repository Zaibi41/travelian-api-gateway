"""Booking itinerary: the flights as held, for the Booking detail's Itinerary tab.

- bookings.itinerary (JSONB, nullable): one entry per flight, in travel order: direction, origin,
  destination, airline, flight number, local departure and arrival, duration, stops, fare name,
  baggage and meal. Taken from the supplier's fresh re-check at hold time. No passenger data.
  NULL for bookings held before this migration (their flights were not recorded).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_booking_itinerary"
down_revision = "0017_saved_people"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("itinerary", postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column("bookings", "itinerary")
