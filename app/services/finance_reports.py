"""Profit and finance reports (travelian#70). One calculation for the booking, the reports and the dashboard tiles.

- A sale is what the dashboard counts as a sale (app.services.admin_dashboard.is_sale), dated by when the booking was
  made, in Pakistan time.
- Profit per booking: app.services.finance_profit.calculate (selling + fees charged - supplier actual charge - payment
  fees - refunds paid). A booking whose cost is not known adds nothing to a profit total and is counted in
  profit_not_known instead (never as 0).
- All sums are integer paisa, PKR (bookings in another currency are left out of money totals and counted apart).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.db import agencies
from app.db.bookings import BookingRow
from app.services.finance_profit import Profit, ProfitInputs, calculate

PKT = ZoneInfo("Asia/Karachi")
CLOSED = ("cancelled", "expired", "hold_failed", "hold_pending", "hold_unrecorded")


@dataclass
class Extras:
    charges: list[int] = field(default_factory=list)
    fees: list[int] = field(default_factory=list)
    card_fees: int = 0
    refunds_paid: list[int] = field(default_factory=list)
    card_payments_without_fee: int = 0
    paid_minor: int = 0


def load_extras(references: Iterable[str]) -> dict[str, Extras]:
    """Supplier charges, payment fees, card fees, paid refunds and money received per booking, in four queries."""
    refs = list(dict.fromkeys(references))
    out: dict[str, Extras] = {ref: Extras() for ref in refs}
    if not refs:
        return out
    with agencies.application_engine().begin() as connection:
        for ref, amount in connection.execute(text(
                "SELECT b.reference, s.amount_minor FROM supplier_charges s JOIN bookings b ON b.id = s.booking_id WHERE b.reference = ANY(:r)"),
                {"r": refs}).all():
            out[ref].charges.append(amount)
        for ref, amount in connection.execute(text(
                "SELECT b.reference, f.amount_minor FROM payment_fees f JOIN booking_payments p ON p.id = f.payment_id"
                " JOIN bookings b ON b.id = p.booking_id WHERE b.reference = ANY(:r)"), {"r": refs}).all():
            out[ref].fees.append(amount)
        for ref, paid, card, missing in connection.execute(text(
                "SELECT b.reference, COALESCE(SUM(p.amount_minor), 0)::bigint, COALESCE(SUM(p.card_fee_minor), 0)::bigint,"
                " count(*) FILTER (WHERE p.method = 'payfast' AND NOT EXISTS (SELECT 1 FROM payment_fees f WHERE f.payment_id = p.id))"
                " FROM booking_payments p JOIN bookings b ON b.id = p.booking_id WHERE b.reference = ANY(:r) GROUP BY b.reference"),
                {"r": refs}).all():
            out[ref].paid_minor, out[ref].card_fees, out[ref].card_payments_without_fee = paid, card, missing
        for ref, amount in connection.execute(text(
                "SELECT b.reference, r.amount_minor FROM refunds r JOIN bookings b ON b.id = r.booking_id"
                " WHERE b.reference = ANY(:r) AND r.status = 'paid'"), {"r": refs}).all():
            out[ref].refunds_paid.append(amount)
    return out


def profit_for(row: BookingRow, extras: Extras) -> Profit:
    return calculate(ProfitInputs(
        selling_minor=row.total_minor or 0, stored_cost_minor=row.supplier_cost_minor,
        cost_known=row.cost_known and not (row.import_source is not None and row.supplier_cost_minor == row.total_minor),
        fees_charged_minor=extras.card_fees, supplier_charges_minor=extras.charges, payment_fees_minor=extras.fees,
        refunds_paid_minor=extras.refunds_paid, card_payments_without_fee=extras.card_payments_without_fee))


def profits(rows: Iterable[BookingRow]) -> dict[str, Profit]:
    rows = list(rows)
    extras = load_extras(row.booking_id for row in rows)
    return {row.booking_id: profit_for(row, extras[row.booking_id]) for row in rows}


@dataclass
class Totals:
    bookings: int = 0
    sales_minor: int = 0
    fees_charged_minor: int = 0
    supplier_charge_minor: int = 0
    payment_fees_minor: int = 0
    refunds_minor: int = 0
    profit_minor: int = 0
    profit_not_known: int = 0
    payment_fees_missing: int = 0
    other_currency: int = 0

    def add(self, row: BookingRow, profit: Profit) -> None:
        self.bookings += 1
        if (row.currency or "PKR").upper() != "PKR":
            self.other_currency += 1
            return
        self.sales_minor += profit.selling_minor
        self.fees_charged_minor += profit.fees_charged_minor
        self.payment_fees_minor += profit.payment_fees_minor
        self.refunds_minor += profit.refunds_minor
        if profit.profit_minor is None:
            self.profit_not_known += 1
        else:
            self.supplier_charge_minor += profit.supplier_charge_minor or 0
            self.profit_minor += profit.profit_minor
        if profit.missing:
            self.payment_fees_missing += 1


def pkt_day(value: datetime) -> date:
    return value.astimezone(PKT).date()


def bucket(day: date, granularity: str) -> date:
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    return day


def sales_in(rows: Iterable[BookingRow], start: date | None, end: date | None, product: str | None = None) -> list[BookingRow]:
    from app.services.admin_dashboard import is_sale

    out = []
    for row in rows:
        if not is_sale(row) or row.created_at is None or (product and row.product != product):
            continue
        day = pkt_day(row.created_at)
        if (start and day < start) or (end and day > end):
            continue
        out.append(row)
    return out


def by_period(sales: list[BookingRow], profit_map: dict[str, Profit], granularity: str) -> list[tuple[date, Totals]]:
    groups: dict[date, Totals] = defaultdict(Totals)
    for row in sales:
        groups[bucket(pkt_day(row.created_at), granularity)].add(row, profit_map[row.booking_id])
    return sorted(groups.items())


def receivables(rows: Iterable[BookingRow], paid: dict[str, int]) -> list[tuple[BookingRow, int]]:
    """Bookings still open (not cancelled or expired) with money still to collect: total - received."""
    out = []
    for row in rows:
        if row.status in CLOSED or row.status in ("requested", "paid_not_ticketed") or not row.total_minor:
            continue
        due = row.total_minor - paid.get(row.booking_id, 0)
        if due > 0:
            out.append((row, due))
    return sorted(out, key=lambda item: -item[1])


def supplier_spend(start: datetime | None, end: datetime | None) -> list[tuple[date, str, int, int]]:
    """(day, provider, ticket charges, top-ups) per PKT day."""
    where, params = "", {}
    if start:
        where += " AND at >= :start"
        params["start"] = start
    if end:
        where += " AND at < :end"
        params["end"] = end
    with agencies.application_engine().begin() as connection:
        return [tuple(r) for r in connection.execute(text(f"""
            SELECT (at AT TIME ZONE 'Asia/Karachi')::date AS day, provider, SUM(charge)::bigint, SUM(top_up)::bigint FROM (
              SELECT recorded_at AS at, COALESCE(provider_id, 'unknown') AS provider, amount_minor AS charge, 0 AS top_up FROM supplier_charges
              UNION ALL
              SELECT occurred_at, provider_id, 0, amount_minor FROM supplier_wallet_entries WHERE kind = 'top_up'
            ) s WHERE true {where} GROUP BY 1, 2 ORDER BY 1, 2"""), params).all()]


def payments_by_method(start: datetime | None, end: datetime | None) -> list[tuple[str, str, int, int, int]]:
    """(method, account, count, amount, card fees) of money received in the range."""
    where, params = "", {}
    if start:
        where += " AND p.received_at >= :start"
        params["start"] = start
    if end:
        where += " AND p.received_at < :end"
        params["end"] = end
    with agencies.application_engine().begin() as connection:
        return [tuple(r) for r in connection.execute(text(
            "SELECT p.method, COALESCE(a.label, p.received_into), count(*), SUM(p.amount_minor)::bigint, SUM(p.card_fee_minor)::bigint"
            " FROM booking_payments p LEFT JOIN payment_accounts a ON a.id = p.account_id"
            f" WHERE p.currency = 'PKR' {where} GROUP BY 1, 2 ORDER BY 1, 2"), params).all()]


def paid_by_booking(references: Iterable[str]) -> dict[str, int]:
    refs = list(dict.fromkeys(references))
    if not refs:
        return {}
    with agencies.application_engine().begin() as connection:
        return dict(connection.execute(text(
            "SELECT b.reference, COALESCE(SUM(p.amount_minor), 0)::bigint FROM bookings b LEFT JOIN booking_payments p ON p.booking_id = b.id"
            " WHERE b.reference = ANY(:r) GROUP BY b.reference"), {"r": refs}).all())
