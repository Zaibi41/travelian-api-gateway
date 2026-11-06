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
from zoneinfo import ZoneInfo

from app.schemas.admin_dashboard import (
    AttentionItem, AttentionQueue, BookingReport, BusinessResults, DashboardFilterOptions,
    DashboardSummary, FilterOption, Money, NeedsAction, Period, Product, ReportBucket,
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


def is_pending_issuance(booking: BookingRow) -> bool:
    return booking.product == "flight" and booking.status in {"payment_received", "confirmed"} and booking.ticket_status != "issued"


def is_payment_pending(booking: BookingRow, now: datetime) -> bool:
    return (
        booking.product == "flight" and booking.status in OPEN_HOLD
        and booking.payment_status != "paid" and not _overdue(booking, now)
    )


def is_expiring_hold(booking: BookingRow, now: datetime, window: timedelta) -> bool:
    return (
        booking.product == "flight" and booking.status in OPEN_HOLD and not _overdue(booking, now)
        and booking.hold_expires_at is not None and booking.hold_expires_at <= now + window
    )


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

    priced = all(booking.currency == "PKR" and booking.total_minor is not None for booking in sales)
    total_sales = Money(amount_minor=sum(booking.total_minor or 0 for booking in sales)) if priced else None
    costed = priced and all(booking.supplier_cost_minor is not None for booking in sales)
    gross_profit = (
        Money(amount_minor=sum((booking.total_minor or 0) - (booking.supplier_cost_minor or 0) for booking in sales))
        if can_see_margin and costed else None
    )

    return DashboardSummary(
        generated_at=now,
        # Any imported row anywhere in the table, regardless of filters.
        contains_sample_data=any(booking.import_source for booking in bookings),
        needs_action=NeedsAction(
            pending_issuance=sum(is_pending_issuance(booking) for booking in selected),
            payment_pending=sum(is_payment_pending(booking, now) for booking in selected),
            expiring_holds=sum(is_expiring_hold(booking, now, window) for booking in selected),
            expiring_window_hours=window_hours,
        ),
        business=BusinessResults(
            total_bookings=len(period_rows),
            issued_tickets=sum(is_issued_ticket(booking) for booking in period_rows),
            total_sales=total_sales,
            gross_profit=gross_profit,
        ),
    )


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
    """Each booking once, under its most urgent queue; soonest deadline first. Ignores the date filter."""
    window = timedelta(hours=window_hours)
    rules = (
        ("hold_expiring", lambda booking: is_expiring_hold(booking, now, window)),
        ("payment_pending", lambda booking: is_payment_pending(booking, now)),
        ("pending_issuance", is_pending_issuance),
    )
    seen: set[str] = set()
    items: list[AttentionItem] = []
    selected = scoped(bookings, query)
    for kind, matches in rules:
        for booking in selected:
            if booking.booking_id in seen or not matches(booking):
                continue
            seen.add(booking.booking_id)
            items.append(AttentionItem(
                booking_id=booking.booking_id, reference=booking.pnr or booking.booking_id,
                type=kind, due_at=booking.hold_expires_at,
            ))
    far_future = datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    items.sort(key=lambda item: item.due_at or far_future)
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
