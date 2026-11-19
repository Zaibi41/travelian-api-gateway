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

from app.core import pii
from app.core.field_visibility import FieldVisibility
from app.db.booking_people import People
from app.db.bookings import BookingRow
from app.services.admin_dashboard import (
    CLOSED, DASHBOARD_PRODUCTS, OPEN_HOLD, attention_kind, is_expiring_hold, is_payment_pending, is_pending_issuance, period_range,
)
from app.services import group_terms
from app.services.group_ticketing_status import ticketing_status
from app.services.pricing import cost_unknown
from app.schemas.admin_bookings import (
    BookingAuditEntry, BookingCustomer, BookingDetail, BookingPayment, BookingFare, BookingList, BookingListCards, BookingListFilterOptions,
    BookingListItem, BookingMoney, BookingPassenger, BookingSegment, BookingStatuses, FarePerPassenger, FilterOption, SupplierPayment,
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
    return booking.payment_status if booking.payment_status in ("paid", "part_paid") else "unpaid"


def issuance_status(booking: BookingRow) -> str:
    if booking.ticket_status in ("unknown", "requested", "processing"):
        return booking.ticket_status
    return "issued" if booking.ticket_status == "issued" or booking.status == "ticket_issued" else "not_issued"


def data_label(booking: BookingRow) -> str | None:
    if booking.import_source is None:
        return None
    return "UAT test data" if booking.import_source == "southair_uat" else "Sample data"


def customer_or_agency(booking: BookingRow) -> str | None:
    """Agency name for agency bookings, otherwise the saved customer's name."""
    return booking.agency_name or booking.customer_name


def _money(amount: int | None, currency: str | None) -> BookingMoney:
    return BookingMoney(amount_minor=amount or 0, currency=(currency or "PKR").upper())


PAID_PENDING = "paid_issuance_pending"
PAID_NEEDS_STAFF = (PAID_PENDING, "paid_not_ticketed")


def booking_status(booking: BookingRow) -> str:
    """The booking status staff see. A flight with an open hold, fully paid and not ticketed is "Paid, issuance pending"
    (derived, never stored: the stored status stays 'held', so issuing works as before)."""
    if (booking.product == "flight" and booking.status in OPEN_HOLD and payment_status(booking) == "paid"
            and issuance_status(booking) in ("not_issued", "requested", "unknown")):
        return PAID_PENDING
    return booking.status


def _statuses(booking: BookingRow) -> BookingStatuses:
    return BookingStatuses(booking=booking_status(booking), payment=payment_status(booking), issuance=issuance_status(booking))


def _in_dates(booking: BookingRow, query: BookingListQuery, now: datetime) -> bool:
    bounds = period_range(query.period, now)
    if bounds is None:
        return True
    value = booking.created_at  # date_type "created"; travel date is not stored yet
    if value is None:
        return False
    start, end, exclusive = bounds
    return start <= value and (value < end if exclusive else value <= end)


def visible_pnr(booking: BookingRow, visibility: FieldVisibility) -> str | None:
    """A group booking's PNR is the supplier's PNR: only with the supplier names permission."""
    return booking.pnr if booking.product != "group" or visibility.supplier_names else None


def _matches_text(booking: BookingRow, q: str, visibility: FieldVisibility) -> bool:
    # Booking ID and PNR only; ticket numbers, passengers and phones are not stored yet.
    return any(q in (value or "").lower() for value in (booking.booking_id, visible_pnr(booking, visibility)))


def visible_to(booking: BookingRow, visibility: FieldVisibility) -> bool:
    """A supplier hold Travelian couldn't record ('hold_unrecorded') only reaches viewers with supplier access."""
    return booking.status != "hold_unrecorded" or visibility.supplier_names


def _in_tab(bookings: Iterable[BookingRow], product: str | None, visibility: FieldVisibility) -> list[BookingRow]:
    return [b for b in bookings if b.product in BOOKING_PRODUCTS and (product is None or b.product == product) and visible_to(b, visibility)]


def _filtered(bookings: list[BookingRow], query: BookingListQuery, now: datetime, visibility: FieldVisibility) -> list[BookingRow]:
    return [
        b for b in bookings
        if _in_dates(b, query, now)
        and (query.q is None or _matches_text(b, query.q, visibility))
        and (query.booking_status is None or booking_status(b) == query.booking_status)
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
        "unpaid": lambda b: payment_status(b) != "paid" and b.status not in CLOSED,  # part-paid still owes money
        "expiring_soon": lambda b: (
            b.status in OPEN_HOLD and b.hold_expires_at is not None and now < b.hold_expires_at <= now + window
        ),
        # The dashboard's needs-action cards open these: the same classification, so the numbers agree.
        "awaiting_issuance": lambda b: attention_kind(b, now, window) == "pending_issuance",
        "hold_deadlines": lambda b: _open_hold(b, now),
        "payment_pending": lambda b: attention_kind(b, now, window) == "payment_pending",
        "paid_not_issued": lambda b: booking_status(b) in PAID_NEEDS_STAFF,
    }


FLIGHT_ONLY_CARDS = {"awaiting_issuance", "hold_deadlines", "payment_pending", "paid_not_issued"}


def build_cards(rows: list[BookingRow], product: str | None, now: datetime, window_hours: int) -> BookingListCards:
    """Counts over the rows in the current filters (spec 7: "total records in the filter")."""
    rules = card_rules(now, window_hours)
    count = lambda name: sum(rules[name](b) for b in rows)  # noqa: E731
    cards = BookingListCards(
        all_bookings=len(rows), confirmed=count("confirmed"), on_hold=count("on_hold"), cancelled=count("cancelled"),
        unpaid=count("unpaid"), expiring_soon=count("expiring_soon"), expiring_window_hours=window_hours,
    )
    if product in ("flight", "group"):
        cards.hold_deadlines = count("hold_deadlines")
    if product == "flight":
        # The dashboard's issuance and payment rules cover flights only; a group tab would show a false 0.
        cards.awaiting_issuance = count("awaiting_issuance")
        cards.payment_pending = count("payment_pending")
        cards.paid_not_issued = count("paid_not_issued")
    return cards


def _sort_key(booking: BookingRow) -> float:
    return -(booking.created_at.timestamp() if booking.created_at else float("-inf"))


def build_list(
    bookings: Iterable[BookingRow], query: BookingListQuery, now: datetime, window_hours: int, visibility: FieldVisibility,
) -> BookingList:
    bookings = list(bookings)
    tab = _in_tab(bookings, query.product, visibility)
    # Cards follow every filter except the card itself, so picking a card doesn't zero the others.
    in_filters = _filtered(tab, query, now, visibility)
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
            booking_id=b.booking_id, pnr=visible_pnr(b, visibility), product=b.product, customer_or_agency=customer_or_agency(b),
            statuses=_statuses(b), amount=_money(b.total_minor, b.currency),
            created_at=b.created_at, expires_at=b.hold_expires_at, sample=is_sample(b), data_label=data_label(b),
        )
        if visibility.supplier_names and b.provider:
            item.provider = b.provider
        items.append(item)

    return BookingList(
        contains_sample_data=any(is_sample(b) for b in bookings),
        cards=build_cards(in_filters, query.product, now, window_hours),
        filter_options=options, total=len(rows), limit=query.limit, offset=query.offset, items=items,
    )


def route_of(itinerary: list[dict] | None) -> str | None:
    """"LHE → DXB → LHE": the airports in travel order, a connection's airport once."""
    if not itinerary:
        return None
    airports: list[str] = []
    for segment in itinerary:
        for code in (segment.get("origin"), segment.get("destination")):
            if code and (not airports or airports[-1] != code):
                airports.append(code)
    return " → ".join(airports) or None


def person_booking(booking: BookingRow, visibility: FieldVisibility, ticket_numbers: list[str]) -> dict:
    """A saved person's booking history row (Customers & Travelers). Cost and profit only with cost and margin."""
    row = {
        "pnr": visible_pnr(booking, visibility), "product": booking.product, "route": route_of(booking.itinerary),
        "departure_local": (booking.itinerary or [{}])[0].get("departure_local"), "statuses": _statuses(booking),
        "ticket_numbers": ticket_numbers, "selling_price": _money(booking.total_minor, booking.currency),
    }
    if visibility.cost_and_margin:
        if cost_unknown(booking.import_source, booking.supplier_cost_minor, booking.total_minor):
            row["cost_unknown"] = True
        else:
            row["supplier_cost"] = _money(booking.supplier_cost_minor, booking.currency)
            row["profit"] = _money((booking.total_minor or 0) - (booking.supplier_cost_minor or 0), booking.currency)
    return row


def find_booking(bookings: Iterable[BookingRow], booking_id: str) -> BookingRow | None:
    return next((b for b in bookings if b.booking_id == booking_id and b.product in BOOKING_PRODUCTS), None)


def build_detail(booking: BookingRow, visibility: FieldVisibility, people: People | None = None, today=None,
                 can_record_payments: bool = False, can_issue_tickets: bool = False) -> BookingDetail:
    billable = _money(booking.total_minor, booking.currency)
    fare = BookingFare(billable_price=billable)
    missing = list(SECTIONS_NOT_AVAILABLE)
    if people and people.travelers:
        missing.remove("passengers")
    if people and people.events:
        missing.remove("audit")
    if people and people.payments:
        missing.remove("payments")
    if people and any(t.ticket_number for t in people.travelers):
        missing.remove("tickets")
    if booking.itinerary:
        missing.remove("itinerary")
    detail = BookingDetail(
        sample=is_sample(booking), sample_label=data_label(booking),
        booking_id=booking.booking_id, pnr=visible_pnr(booking, visibility), product=booking.product,
        customer_or_agency=customer_or_agency(booking), statuses=_statuses(booking), amount=billable,
        deadline=booking.hold_expires_at, created_at=booking.created_at, updated_at=booking.updated_at,
        fare=fare, sections_not_available=missing,
        can_record_payments=can_record_payments, can_issue_tickets=can_issue_tickets,
    )
    paid = sum(p.amount_minor for p in people.payments) if people else 0
    if people is not None:
        detail.amount_paid = _money(paid, booking.currency)
        detail.amount_due = _money(max((booking.total_minor or 0) - paid, 0), booking.currency)
    if people and people.payments:
        detail.payments = [
            BookingPayment(id=p.id, amount=_money(p.amount_minor, p.currency), method=p.method, received_into=p.received_into,
                           reference=p.reference, received_at=p.received_at, recorded_by_user_id=p.recorded_by_user_id,
                           recorded_by_role=p.recorded_by_role, recorded_at=p.recorded_at)
            for p in people.payments
        ]
    if people and people.customer:
        c = people.customer
        detail.customer = BookingCustomer(first_name=c.first_name, last_name=c.last_name, phone=f"+{c.phone_code} {c.phone}", email=c.email)
    if people and people.travelers:
        detail.passengers = [
            BookingPassenger(
                position=t.position, type=t.passenger_type, title=t.title, first_name=t.first_name, last_name=t.last_name,
                date_of_birth=t.date_of_birth, id_type=t.id_type, document_number=t.document_number,
                document_country=t.document_country, nationality=t.nationality, document_expiry=t.document_expiry,
                document_validity=pii.document_validity(t.document_expiry, today or datetime.now().date()),
                infant_of_position=t.infant_of_position, ticket_number=t.ticket_number,
            )
            for t in people.travelers
        ]
    if booking.itinerary:
        detail.itinerary = [BookingSegment.model_validate(segment) for segment in booking.itinerary]
    if people and people.events:
        from app.services.group_holds import fare_terms_view  # late: group_holds imports this module's helpers
        detail.fare_terms = fare_terms_view(group_terms.acknowledgement(people.events))
        detail.audit = [BookingAuditEntry(action=e.action, actor_user_id=e.actor_user_id, actor_role=e.actor_role, at=e.created_at, details=e.details)
                        for e in people.events]
    unknown_cost = cost_unknown(booking.import_source, booking.supplier_cost_minor, booking.total_minor)
    if people and people.travelers and all(t.selling_minor is not None for t in people.travelers):
        per_type: dict[str, list] = {}
        for traveler in people.travelers:
            per_type.setdefault(traveler.passenger_type, []).append(traveler)
        fare.per_passenger = []
        for kind in ("ADT", "CHD", "INF"):
            if kind in per_type:
                group = per_type[kind]
                item = FarePerPassenger(passenger_type=kind, count=len(group), selling_each=_money(max(t.selling_minor for t in group), booking.currency))
                if visibility.cost_and_margin and not unknown_cost and all(t.cost_minor is not None for t in group):
                    item.supplier_cost_each = _money(max(t.cost_minor for t in group), booking.currency)
                fare.per_passenger.append(item)
    if booking.product == "group":
        due_zero = people is not None and paid >= (booking.total_minor or 0)
        detail.final_pnr = booking.final_pnr
        detail.group_ticketing_status = ticketing_status(booking.status, booking.ticket_status, booking.final_pnr, due_zero)
        detail.itinerary_available = detail.group_ticketing_status == "ticketed"
        detail.can_edit_group_tickets = (can_issue_tickets and due_zero and booking.status in ("held", "confirmed", "ticket_issued")
                                         and not (booking.status == "held" and booking.hold_expires_at is not None
                                                  and booking.hold_expires_at <= datetime.now(booking.hold_expires_at.tzinfo)))
    detail.can_set_selling_price = (can_record_payments and booking.status in ("held", "pending_payment") and issuance_status(booking) == "not_issued"
                                    and not (people and people.payments) and bool(fare.per_passenger))
    if visibility.supplier_names and booking.provider:
        detail.provider = booking.provider
    if visibility.cost_and_margin:
        if unknown_cost:
            fare.cost_unknown = True
        else:
            cost = _money(booking.supplier_cost_minor, booking.currency)
            fare.supplier_cost = cost
            if booking.total_minor is not None:
                fare.margin = _money(booking.total_minor - booking.supplier_cost_minor, booking.currency)
        detail.supplier_payment = SupplierPayment(
            amount_due=fare.supplier_cost, hold_deadline=booking.hold_expires_at,
        )
    return detail
