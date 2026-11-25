"""Finance > Payments (travelian#69): every incoming payment across bookings, plus slips awaiting a check or rejected.

A confirmed payment is a booking_payments row (its slip, if any, is linked). A slip that is still awaiting a check or was
rejected is listed as its own row. All sums are integer paisa, worked out in SQL here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text

from app.db import agencies

STATUSES = ("awaiting_check", "confirmed", "rejected")
METHODS = ("bank_transfer", "jazzcash", "1link", "payfast", "alfalah")

_ROWS = """
WITH rows AS (
  SELECT 'payment' AS kind, p.id, b.reference AS booking_reference, b.pnr, b.product, b.is_test,
         NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), '') AS customer_name,
         p.method, p.account_id, COALESCE(a.label, p.received_into) AS account_label,
         p.amount_minor, p.card_fee_minor,
         (SELECT COALESCE(SUM(f.amount_minor), 0) FROM payment_fees f WHERE f.payment_id = p.id)::bigint AS payment_fee_minor,
         EXISTS (SELECT 1 FROM payment_fees f WHERE f.payment_id = p.id) AS fee_recorded,
         p.currency, p.reference AS payer_reference, 'confirmed' AS status, p.received_at AS at, s.id AS slip_id,
         NULL::text AS rejection_reason, b.chosen_account_id
  FROM booking_payments p JOIN bookings b ON b.id = p.booking_id
  LEFT JOIN booking_customers c ON c.booking_id = b.id
  LEFT JOIN payment_accounts a ON a.id = p.account_id
  LEFT JOIN payment_slips s ON s.payment_id = p.id
  UNION ALL
  SELECT 'slip', s.id, b.reference, b.pnr, b.product, b.is_test,
         NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), ''),
         s.method, s.account_id, a.label, s.amount_minor, 0::bigint, 0::bigint, false,
         s.currency, s.payer_reference, s.status, s.submitted_at, s.id, s.rejection_reason, b.chosen_account_id
  FROM payment_slips s JOIN bookings b ON b.id = s.booking_id
  LEFT JOIN booking_customers c ON c.booking_id = b.id
  LEFT JOIN payment_accounts a ON a.id = s.account_id
  WHERE s.status <> 'confirmed'
)
"""


@dataclass(frozen=True)
class PaymentQuery:
    date_from: datetime | None = None
    date_to: datetime | None = None
    method: str | None = None
    status: str | None = None
    account_id: str | None = None
    q: str | None = None


def _where(query: PaymentQuery) -> tuple[str, dict[str, Any]]:
    where, params = ["true"], {}
    if query.date_from:
        where.append("at >= :from")
        params["from"] = query.date_from
    if query.date_to:
        where.append("at < :to")
        params["to"] = query.date_to
    if query.method:
        where.append("method = :method")
        params["method"] = query.method
    if query.status:
        where.append("status = :status")
        params["status"] = query.status
    if query.account_id:
        where.append("account_id = :account")
        params["account"] = query.account_id
    if query.q and query.q.strip():
        where.append("(booking_reference ILIKE :q OR pnr ILIKE :q OR customer_name ILIKE :q OR payer_reference ILIKE :q)")
        params["q"] = f"%{query.q.strip().replace('%', '').replace('_', ' ')}%"
    return " AND ".join(where), params


@dataclass(frozen=True)
class DayTotal:
    day: date
    method: str
    count: int
    amount_minor: int
    card_fee_minor: int


@dataclass(frozen=True)
class PaymentPage:
    rows: list[dict]
    total: int
    confirmed_minor: int
    confirmed_count: int
    awaiting_minor: int
    awaiting_count: int
    days: list[DayTotal]


def list_payments(query: PaymentQuery, limit: int = 50, offset: int = 0) -> PaymentPage:
    clause, params = _where(query)
    with agencies.application_engine().begin() as connection:
        rows = [dict(r) for r in connection.execute(text(
            _ROWS + f"SELECT * FROM rows WHERE {clause} ORDER BY at DESC, id LIMIT :limit OFFSET :offset"),
            {**params, "limit": limit, "offset": offset}).mappings().all()]
        summary = connection.execute(text(
            _ROWS + "SELECT count(*) AS total,"
            " COALESCE(SUM(amount_minor) FILTER (WHERE status = 'confirmed'), 0)::bigint AS confirmed_minor,"
            " count(*) FILTER (WHERE status = 'confirmed') AS confirmed_count,"
            " COALESCE(SUM(amount_minor) FILTER (WHERE status = 'awaiting_check'), 0)::bigint AS awaiting_minor,"
            " count(*) FILTER (WHERE status = 'awaiting_check') AS awaiting_count"
            f" FROM rows WHERE {clause}"), params).mappings().one()
        days = connection.execute(text(
            _ROWS + "SELECT (at AT TIME ZONE 'Asia/Karachi')::date AS day, method, count(*) AS n, SUM(amount_minor)::bigint AS amount,"
            f" SUM(card_fee_minor)::bigint AS fee FROM rows WHERE {clause} AND status = 'confirmed'"
            " GROUP BY 1, 2 ORDER BY 1 DESC, 2 LIMIT 400"), params).all()
    return PaymentPage(rows, summary["total"], summary["confirmed_minor"], summary["confirmed_count"], summary["awaiting_minor"],
                       summary["awaiting_count"], [DayTotal(d, m, n, a, f) for d, m, n, a, f in days])


def export_rows(query: PaymentQuery, cap: int = 20_000) -> list[dict]:
    clause, params = _where(query)
    with agencies.application_engine().begin() as connection:
        return [dict(r) for r in connection.execute(text(_ROWS + f"SELECT * FROM rows WHERE {clause} ORDER BY at DESC, id LIMIT :cap"),
                                                    {**params, "cap": cap}).mappings().all()]


def refunds_due() -> list[dict]:
    """Paid bookings that can't be travelled on (paid, not ticketed; cancelled; expired) with money still refundable and
    no refund open for it yet. Amounts are the fare money received (card fees are not refunded, travelian#74)."""
    with agencies.application_engine().begin() as connection:
        return [dict(r) for r in connection.execute(text("""
            SELECT b.reference AS booking_reference, b.pnr, b.product, b.booking_status, b.currency, b.is_test,
                   NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), '') AS customer_name,
                   b.supplier_response -> 'action_needed' ->> 'reason' AS reason, b.updated_at,
                   paid.amount AS paid_minor,
                   (paid.amount - COALESCE((SELECT SUM(r.amount_minor) FROM refunds r WHERE r.booking_id = b.id AND r.status <> 'rejected'), 0))::bigint
                     AS refundable_minor
            FROM bookings b
            JOIN LATERAL (SELECT COALESCE(SUM(p.amount_minor), 0)::bigint AS amount FROM booking_payments p WHERE p.booking_id = b.id) paid ON true
            LEFT JOIN booking_customers c ON c.booking_id = b.id
            WHERE b.booking_status IN ('paid_not_ticketed', 'cancelled', 'expired') AND paid.amount > 0
              AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.booking_id = b.id AND r.status IN ('requested', 'approved'))
              AND paid.amount - COALESCE((SELECT SUM(r.amount_minor) FROM refunds r WHERE r.booking_id = b.id AND r.status <> 'rejected'), 0) > 0
            ORDER BY b.updated_at DESC LIMIT 500""")).mappings().all()]
