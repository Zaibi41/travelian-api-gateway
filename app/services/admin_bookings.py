"""Admin Bookings list and Booking detail, computed server-side.

Field visibility (spec v3.0 D23) is applied while building the response:
supplier names (`provider`, provider filter options) and cost/margin
(`fare.supplier_cost`, `fare.margin`, `supplier_payment`) are never set for a
viewer without the matching permission, so they are absent from the JSON.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.field_visibility import FieldVisibility
from app.db.bookings import BookingRow
from app.services.admin_dashboard import (
    CLOSED, DASHBOARD_PRODUCTS, OPEN_HOLD, is_expiring_hold, is_pending_issuance, period_range,
)
from app.schemas.admin_bookings import (
    BookingDetail, BookingFare, BookingList, BookingListCards, BookingListFilterOptions, BookingListItem,
    BookingMoney, BookingStatuses, FilterOption, SupplierPayment,
)

#: Bookings tabs. "group" has no rows in the bookings table yet, but the tab exists.
BOOKING_PRODUCTS = {**DASHBOARD_PRODUCTS, "group": "Group Ticketing"}

#: Detail tabs whose data is not recorded in the gateway yet.
SECTIONS_NOT_AVAILABLE = ["itinerary", "passengers", "payments", "tickets", "servicing", "audit"]


@dataclass(frozen=True)
class BookingListQuery:
    product: str | None = None
    period: str = "all"
    date_type: str = "created"
    q: str | None = None  # lower-case
    booking_status: str | None = None
    payment_status: str | None = None
    issuance_status: str | None = None
    provider: str | None = None  # lower-case; only when the viewer may see supplier names
    agency: str | None = None  # agency id
    card: str | None = None  # a card's own rule, see CARD_RULES
    limit: int = 50
    offset: int = 0


def is_sample(booking: BookingRow) -> bool:
    return booking.import_source is not None


def payment_status(booking: BookingRow) -> str:
    return "paid" if booking.payment_status == "paid" else "unpaid"


def issuance_status(booking: BookingRow) -> str:
    return "issued" if booking.ticket_status == "issued" or booking.status == "ticket_issued" else "not_issued"


def _money(amount: int | None, currency: str | None) -> BookingMoney:
    return BookingMoney(amount_minor=amount or 0, currency=(currency or "PKR").upper())


def _statuses(booking: BookingRow) -> BookingStatuses:
    return BookingStatuses(booking=booking.status, payment=payment_status(booking), issuance=issuance_status(booking))


def _in_dates(booking: BookingRow, query: BookingListQuery, now: datetime) -> bool:
    bounds = period_range(query.period, now)
    if bounds is None:
        return True
    value = booking.created_at  # date_type "created"; travel date is not stored yet
    if value is None:
        return False
    start, end, exclusive = bounds
    return start <= value and (value < end if exclusive else value <= end)


def _matches_text(booking: BookingRow, q: str) -> bool:
    # Booking ID and PNR only; ticket numbers, passengers and phones are not stored yet.
    return any(q in (value or "").lower() for value in (booking.booking_id, booking.pnr))


def _in_tab(bookings: Iterable[BookingRow], product: str | None) -> list[BookingRow]:
    return [b for b in bookings if b.product in BOOKING_PRODUCTS and (product is None or b.product == product)]


def _filtered(bookings: list[BookingRow], query: BookingListQuery, now: datetime) -> list[BookingRow]:
    return [
        b for b in bookings
        if _in_dates(b, query, now)
        and (query.q is None or _matches_text(b, query.q))
        and (query.booking_status is None or b.status == query.booking_status)
        and (query.payment_status is None or payment_status(b) == query.payment_status)
        and (query.issuance_status is None or issuance_status(b) == query.issuance_status)
        and (query.provider is None or (b.provider or "").lower() == query.provider)
        and (query.agency is None or b.agency_id == query.agency)
    ]


def _open_hold(booking: BookingRow, now: datetime) -> bool:
    """Active hold with a deadline (spec 7: On Hold)."""
    return booking.status in OPEN_HOLD and booking.hold_expires_at is not None and booking.hold_expires_at > now


def card_rules(now: datetime, window_hours: int):
    """Each card's rule. A card counts, and its filter lists, exactly these rows."""
    window = timedelta(hours=window_hours)
    return {
        "confirmed": lambda b: b.status == "confirmed",
        "on_hold": lambda b: _open_hold(b, now),
        "cancelled": lambda b: b.status in CLOSED,  # cancelled or expired
        "unpaid": lambda b: payment_status(b) == "unpaid" and b.status not in CLOSED,
        "expiring_soon": lambda b: (
            b.status in OPEN_HOLD and b.hold_expires_at is not None and now < b.hold_expires_at <= now + window
        ),
        "awaiting_issuance": is_pending_issuance,
        "hold_deadlines": lambda b: _open_hold(b, now),
    }


FLIGHT_ONLY_CARDS = {"awaiting_issuance", "hold_deadlines"}


def build_cards(rows: list[BookingRow], product: str | None, now: datetime, window_hours: int) -> BookingListCards:
    """Counts over the rows in the current filters (spec 7: "total records in the filter")."""
    rules = card_rules(now, window_hours)
    count = lambda name: sum(rules[name](b) for b in rows)  # noqa: E731
    cards = BookingListCards(
        all_bookings=len(rows), confirmed=count("confirmed"), on_hold=count("on_hold"), cancelled=count("cancelled"),
        unpaid=count("unpaid"), expiring_soon=count("expiring_soon"), expiring_window_hours=window_hours,
    )
    if product == "flight":
        cards.awaiting_issuance = count("awaiting_issuance")
        cards.hold_deadlines = count("hold_deadlines")
    return cards


def _sort_key(booking: BookingRow) -> float:
    return -(booking.created_at.timestamp() if booking.created_at else float("-inf"))


def build_list(
    bookings: Iterable[BookingRow], query: BookingListQuery, now: datetime, window_hours: int, visibility: FieldVisibility,
) -> BookingList:
    bookings = list(bookings)
    tab = _in_tab(bookings, query.product)
    # Cards follow every filter except the card itself, so picking a card doesn't zero the others.
    in_filters = _filtered(tab, query, now)
    card_rule = card_rules(now, window_hours).get(query.card) if query.card else None
    rows = sorted([b for b in in_filters if card_rule is None or card_rule(b)], key=_sort_key)
    page = rows[query.offset:query.offset + query.limit]

    agencies = sorted({(b.agency_id, b.agency_name or b.agency_id) for b in tab if b.agency_id}, key=lambda a: a[1].lower())
    options = BookingListFilterOptions(agencies=[FilterOption(value=v, label=l) for v, l in agencies])
    if visibility.supplier_names:
        names = {(b.provider or "").strip() for b in tab} - {""}
        options.providers = [FilterOption(value=n.lower(), label=n) for n in sorted(names, key=str.lower)]

    items = []
    for b in page:
        item = BookingListItem(
            booking_id=b.booking_id, pnr=b.pnr, product=b.product, customer_or_agency=b.agency_name,
            statuses=_statuses(b), amount=_money(b.total_minor, b.currency),
            created_at=b.created_at, expires_at=b.hold_expires_at, sample=is_sample(b),
        )
        if visibility.supplier_names and b.provider:
            item.provider = b.provider
        items.append(item)

    return BookingList(
        contains_sample_data=any(is_sample(b) for b in bookings),
        cards=build_cards(in_filters, query.product, now, window_hours),
        filter_options=options, total=len(rows), limit=query.limit, offset=query.offset, items=items,
    )


def find_booking(bookings: Iterable[BookingRow], booking_id: str) -> BookingRow | None:
    return next((b for b in bookings if b.booking_id == booking_id and b.product in BOOKING_PRODUCTS), None)


def build_detail(booking: BookingRow, visibility: FieldVisibility) -> BookingDetail:
    billable = _money(booking.total_minor, booking.currency)
    fare = BookingFare(billable_price=billable)
    detail = BookingDetail(
        sample=is_sample(booking), sample_label="Sample data" if is_sample(booking) else None,
        booking_id=booking.booking_id, pnr=booking.pnr, product=booking.product,
        customer_or_agency=booking.agency_name, statuses=_statuses(booking), amount=billable,
        deadline=booking.hold_expires_at, created_at=booking.created_at, updated_at=booking.updated_at,
        fare=fare, sections_not_available=list(SECTIONS_NOT_AVAILABLE),
    )
    if visibility.supplier_names and booking.provider:
        detail.provider = booking.provider
    if visibility.cost_and_margin:
        if booking.supplier_cost_minor is not None:
            cost = _money(booking.supplier_cost_minor, booking.currency)
            fare.supplier_cost = cost
            if booking.total_minor is not None:
                fare.margin = _money(booking.total_minor - booking.supplier_cost_minor, booking.currency)
        detail.supplier_payment = SupplierPayment(
            amount_due=fare.supplier_cost, hold_deadline=booking.hold_expires_at,
        )
    return detail
