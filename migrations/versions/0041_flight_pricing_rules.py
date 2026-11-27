"""Flight pricing rules (Pricing & Revenue > Flight Pricing & Rules): an amount in Rs per passenger added to (or, when
negative, taken off) the selling price of one supplier's fares, optionally only one SooperFare source and/or one airline.
Additive only.

- supplier: southair, sooperfare or kyte. source: SooperFare's own source (PK, PA, PF, ONEAPI, Z8, SABRE), only for
  sooperfare; NULL = any. airline_code: the airline (IATA); NULL = any.
- amount_minor: whole rupees in paisa, not zero, at most Rs 100,000 either way. A selling price never goes below the
  supplier's cost (app.services.live_flights.sell).
- One rule per (supplier, source, airline); the most specific matching rule applies (app.services.flight_pricing_rules).
"""

import sqlalchemy as sa
from alembic import op

revision = "0041_flight_pricing_rules"
down_revision = "0040_ticket_sale_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flight_pricing_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("supplier", sa.String(20), nullable=False),
        sa.Column("source", sa.String(20), nullable=True),
        sa.Column("airline_code", sa.String(3), nullable=True),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("updated_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("supplier IN ('southair', 'sooperfare', 'kyte')", name="ck_fpr_supplier"),
        sa.CheckConstraint("source IS NULL OR supplier = 'sooperfare'", name="ck_fpr_source"),
        sa.CheckConstraint("airline_code IS NULL OR airline_code ~ '^[A-Z0-9]{2,3}$'", name="ck_fpr_airline"),
        sa.CheckConstraint("amount_minor <> 0 AND amount_minor % 100 = 0 AND amount_minor BETWEEN -10000000 AND 10000000",
                           name="ck_fpr_amount"),
    )
    op.create_index("ux_fpr_scope", "flight_pricing_rules",
                    ["supplier", sa.text("coalesce(source, '')"), sa.text("coalesce(airline_code, '')")], unique=True)


def downgrade() -> None:
    op.drop_index("ux_fpr_scope", table_name="flight_pricing_rules")
    op.drop_table("flight_pricing_rules")
