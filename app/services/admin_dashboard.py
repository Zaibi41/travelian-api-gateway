"""Admin Dashboard home figures, computed server-side.

Queue rules mirror the portal's Flight Operations queues
(src/lib/flight-operation-queues.ts) so card counts match those lists, except
the expiring-holds window, which is the DASHBOARD_EXPIRING_HOLD_HOURS setting.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.services.pricing import cost_unknown
from app.schemas.admin_dashboard import (
    AttentionItem, AttentionQueue, BookingReport, BusinessResults, DashboardFilterOptions,
    CurrencyAmount, DashboardSummary, FilterOption, NeedsAction, Period, Product, ReportBucket,
)
from app.db.bookings import BookingRow

DASHBOARD_PRODUCTS: dict[str, str] = {"flight": "Flights", "hotel": "Hotels", "umrah": "Umrah"}
OPEN_HOLD = {"held", "pending_payment"}
CLOSED = {"cancelled", "expired"}


@dataclass(frozen=True)
class DashboardQuery:
    period: Period = "all"
    product: Product | None = None
    provider: str | None = None  # lower-case provider name


# ---------- periods (local calendar of DASHBOARD_TIMEZONE) ----------

def _start_of_day(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year, month = value.year + month_index // 12, month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def period_range(period: Period, now: datetime) -> tuple[datetime, datetime, bool] | None:
    """(start, end, end_exclusive) in now's timezone; None for "all"."""
    today = _start_of_day(now)
    month = today.replace(day=1)
    year = month.replace(month=1)
    match period:
        case "all":
            return None
        case "today":
            return today, now, False
        case "yesterday":
            return today - timedelta(days=1), today, True
        case "last-week":
            return today - timedelta(days=6), now, False
        case "last-two-weeks":
            return today - timedelta(days=13), now, False
        case "this-month":
            return month, now, False
        case "last-month":
            return _add_months(month, -1), month, True
        case "last-three-months":
            return _start_of_day(_add_months(now, -3)), now, False
        case "last-six-months":
            return _start_of_day(_add_months(now, -6)), now, False
        case "this-year":
            return year, now, False
        case "last-year":
            return year.replace(year=year.year - 1), year, True
    raise ValueError(f"Unknown period {period!r}")


def in_period(booking: BookingRow, period: Period, now: datetime) -> bool:
    bounds = period_range(period, now)
    if bounds is None:
        return True
    if booking.created_at is None:
        return False
    start, end, exclusive = bounds
    return start <= booking.created_at and (booking.created_at < end if exclusive else booking.created_at <= end)


# ---------- queue rules ----------

def _overdue(booking: BookingRow, now: datetime) -> bool:
    return booking.status in OPEN_HOLD and booking.hold_expires_at is not None and booking.hold_expires_at <= now


def due_ahead(booking: BookingRow, now: datetime) -> bool:
    """The booking's due time (hold expiry) is still in the future. Needs-action cards and the Attention Queue only
    list these: a past due time is never listed, whatever the stored status (server time; aware datetimes compare in UTC)."""
    return booking.hold_expires_at is not None and booking.hold_expires_at > now


def is_pending_issuance(booking: BookingRow, now: datetime) -> bool:
    """Paid or confirmed and waiting for a ticket: also a fully paid hold, or one with issuance
    requested or in progress, while its due time is ahead. Issued and unknown issuance are counted elsewhere."""
    if booking.product != "flight" or booking.ticket_status in ("issued", "unknown") or not due_ahead(booking, now):
        return False
    return (booking.status in {"payment_received", "confirmed"} or booking.ticket_status in ("requested", "processing")
            or (booking.status in OPEN_HOLD and booking.payment_status == "paid"))


def is_payment_pending(booking: BookingRow, now: datetime) -> bool:
    return (
        booking.product == "flight" and booking.status in OPEN_HOLD
        and booking.payment_status != "paid" and due_ahead(booking, now)
    )


def is_expiring_hold(booking: BookingRow, now: datetime, window: timedelta) -> bool:
    return (
        booking.product == "flight" and booking.status in OPEN_HOLD and not _overdue(booking, now)
        and booking.hold_expires_at is not None and booking.hold_expires_at <= now + window
    )


#: Most urgent first: each booking is listed once, under the first kind it matches.
ATTENTION_KINDS = ("hold_expiring", "payment_pending", "pending_issuance")


def attention_kind(booking: BookingRow, now: datetime, window: timedelta) -> str | None:
    """The one Attention Queue kind of a booking (None: not listed). The needs-action cards count exactly these, so a
    card's number equals its rows in the queue."""
    if not due_ahead(booking, now):
        return None
    if is_expiring_hold(booking, now, window):
        return "hold_expiring"
    if is_payment_pending(booking, now):
        return "payment_pending"
    if is_pending_issuance(booking, now):
        return "pending_issuance"
    return None


def is_issued_ticket(booking: BookingRow) -> bool:
    return booking.product == "flight" and booking.status not in CLOSED and (
        booking.status == "ticket_issued" or booking.ticket_status == "issued"
    )


def is_sale(booking: BookingRow) -> bool:
    return booking.status not in CLOSED and (
        booking.payment_status == "paid" or booking.status in {"payment_received", "ticket_issued"}
        or booking.ticket_status == "issued"
    )


def scoped(bookings: Iterable[BookingRow], query: DashboardQuery) -> list[BookingRow]:
    """Dashboard products only (retired Tour/Visa excluded), then product/provider filters."""
    return [
        booking for booking in bookings
        if booking.product in DASHBOARD_PRODUCTS
        and (query.product is None or booking.product == query.product)
        and (query.provider is None or (booking.provider or "").lower() == query.provider)
    ]


# ---------- responses ----------

def build_summary(
    bookings: Iterable[BookingRow], query: DashboardQuery, now: datetime, window_hours: int, can_see_margin: bool,
) -> DashboardSummary:
    bookings = list(bookings)  # iterated more than once below
    window = timedelta(hours=window_hours)
    selected = scoped(bookings, query)
    period_rows = [booking for booking in selected if in_period(booking, query.period, now)]
    sales = [booking for booking in period_rows if is_sale(booking)]

    total_sales, gross_profit = money_by_currency(sales)

    return DashboardSummary(
        generated_at=now,
        # Any imported row anywhere in the table, regardless of filters.
        contains_sample_data=any(booking.import_source for booking in bookings),
        needs_action=NeedsAction(
            # The same classification as the Attention Queue: a card's number equals its rows there.
            pending_issuance=sum(attention_kind(booking, now, window) == "pending_issuance" for booking in selected),
            payment_pending=sum(attention_kind(booking, now, window) == "payment_pending" for booking in selected),
            expiring_holds=sum(attention_kind(booking, now, window) == "hold_expiring" for booking in selected),
            expiring_window_hours=window_hours,
        ),
        business=BusinessResults(
            total_bookings=len(period_rows),
            issued_tickets=sum(is_issued_ticket(booking) for booking in period_rows),
            total_sales=total_sales,
            gross_profit=gross_profit if can_see_margin else None,
        ),
    )


def money_by_currency(sales: list[BookingRow]) -> tuple[list[CurrencyAmount], list[CurrencyAmount]]:
    """Sales and gross profit summed per currency (PKR first, then A-Z; PKR 0 when there are no sales). A sum is null
    (n/a) when a booking in it has no total, or for profit, no real supplier cost; never a guessed 0."""
    by_currency: dict[str, list[BookingRow]] = {"PKR": []} if not sales else {}
    for booking in sales:
        by_currency.setdefault((booking.currency or "PKR").upper(), []).append(booking)
    sales_out, profit_out = [], []
    for currency in sorted(by_currency, key=lambda c: (c != "PKR", c)):
        rows = by_currency[currency]
        priced = all(b.total_minor is not None for b in rows)
        costed = priced and not any(cost_unknown(b.import_source, b.supplier_cost_minor, b.total_minor) for b in rows)
        sales_out.append(CurrencyAmount(currency=currency, amount_minor=sum(b.total_minor or 0 for b in rows) if priced else None))
        profit_out.append(CurrencyAmount(currency=currency, amount_minor=sum((b.total_minor or 0) - (b.supplier_cost_minor or 0) for b in rows)
                                         if costed else None))
    return sales_out, profit_out


def report_granularity(days: int) -> str:
    if days <= 31:
        return "day"
    if days <= 183:
        return "week"
    return "month"


def _bucket_start(value: date, granularity: str) -> date:
    if granularity == "day":
        return value
    if granularity == "week":
        return value - timedelta(days=value.weekday())  # Monday
    return value.replace(day=1)


def _next(value: date, granularity: str) -> date:
    if granularity == "day":
        return value + timedelta(days=1)
    if granularity == "week":
        return value + timedelta(days=7)
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def build_booking_report(bookings: Iterable[BookingRow], period: Period, now: datetime) -> BookingReport:
    """Follows the date filter only; every bucket in a bounded period is returned."""
    tz = now.tzinfo
    rows = [
        booking for booking in scoped(bookings, DashboardQuery())
        if booking.created_at is not None and in_period(booking, period, now)
    ]
    if not rows:
        return BookingReport(granularity="day", timezone=str(tz), buckets=[])

    local_days = [booking.created_at.astimezone(tz).date() for booking in rows]
    bounds = period_range(period, now)
    if bounds:
        start, end, exclusive = bounds
        first, last = start.date(), (end - timedelta(microseconds=1) if exclusive else end).date()
    else:
        first, last = min(local_days), max(local_days)
    granularity = report_granularity((last - first).days + 1)

    buckets: dict[date, ReportBucket] = {}
    cursor = _bucket_start(first, granularity)
    while cursor <= last:
        buckets[cursor] = ReportBucket(start=cursor, bookings=0, issued=0)
        cursor = _next(cursor, granularity)
    for booking, day in zip(rows, local_days):
        bucket = buckets.get(_bucket_start(day, granularity))
        if bucket is None:
            continue
        bucket.bookings += 1
        bucket.issued += int(is_issued_ticket(booking))
    return BookingReport(granularity=granularity, timezone=str(tz), buckets=list(buckets.values()))


def build_attention_queue(
    bookings: Iterable[BookingRow], query: DashboardQuery, now: datetime, window_hours: int, limit: int,
) -> AttentionQueue:
    """Each booking once, under its most urgent kind, only while its due time is ahead; soonest due first.
    Ignores the date filter."""
    window = timedelta(hours=window_hours)
    items: list[AttentionItem] = []
    for booking in scoped(bookings, query):
        kind = attention_kind(booking, now, window)
        if kind is not None:
            items.append(AttentionItem(booking_id=booking.booking_id, reference=booking.pnr or booking.booking_id,
                                       type=kind, due_at=booking.hold_expires_at))
    items.sort(key=lambda item: (item.due_at, ATTENTION_KINDS.index(item.type), item.booking_id))
    return AttentionQueue(total=len(items), items=items[:limit])


def build_filter_options(bookings: Iterable[BookingRow]) -> DashboardFilterOptions:
    providers: dict[str, str] = {}
    for booking in scoped(bookings, DashboardQuery()):
        if booking.provider:
            providers.setdefault(booking.provider.lower(), booking.provider)
    return DashboardFilterOptions(
        products=[FilterOption(value=value, label=label) for value, label in DASHBOARD_PRODUCTS.items()],
        providers=[FilterOption(value=value, label=label) for value, label in sorted(providers.items(), key=lambda pair: pair[1].lower())],
    )
