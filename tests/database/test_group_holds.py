"""Group Ticketing holds against a fake hold provider: hold (price only after it), failures, adults
only, release, price re-check (changed: refused until the customer agrees), payment before issue,
expiry, supplier name hidden from staff, saved customer found by document number."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, resolve_actions
from app.db import booking_payments, payment_accounts, saved_people
from app.schemas.group_holds import GroupHoldRequest
from app.services import group_holds
from app.services.group_holds import GroupHoldError, HeldSeats, SupplierRefused
from app.services.group_ticketing import group_ticket_id
from app.services.group_ticketing_view import GroupMoney
from app.vendors.group_ticketing.dhillu_sahi.groups import normalize_groups
from tests.database.test_booking_people import ACTOR

ADMIN = BackOfficeUser(ACTOR, "super_admin", ALL, resolve_actions("super_admin", set()))
STAFF = BackOfficeUser(ACTOR, "staff", NONE)  # no permissions: can't record payments or issue
VENDOR = "dhillu_sahi"
SUPPLIER_REF = "SUPPLIER-HOLD-SECRET-77"


def inventory(remaining=12):
    return normalize_groups([{"sector": "ISB - JED", "airline": {"name": "Fly Jinnah", "logo": None}, "tickets": [{
        "pnr": "SUPPNR1", "date": "2026-11-22", "price": "100000", "remaining": remaining, "baggage": "20+7",
        "subpart": [{"departure_time": "06:40"}]}]}])


TICKET_ID = group_ticket_id(inventory()[0].tickets[0])


class FakeProvider:
    """A provider that holds, prices and releases; `supports_issue` lets the generic issue path be tested
    (no real group provider issues yet)."""

    issue_unavailable_reason = group_holds.NO_ISSUE_REASON

    def __init__(self, cost=100_000_00, issue_answer=None, hold_error=None, supports_issue=True, release_errors=0):
        self.cost = cost
        self.issue_answer = issue_answer
        self.hold_error = hold_error
        self.supports_issue = supports_issue
        self.release_errors = release_errors
        self.calls = []

    async def hold(self, ticket, passengers, contact):
        self.calls.append(("hold", len(passengers)))
        if self.hold_error:
            raise self.hold_error
        return HeldSeats("K7P2QX", SUPPLIER_REF, GroupMoney(amount_minor=self.cost, currency="PKR"))

    async def price(self, held):
        self.calls.append("price")
        return GroupMoney(amount_minor=self.cost, currency="PKR")

    async def release(self, held):
        self.calls.append(("release", held.supplier_ref))
        if self.release_errors:
            self.release_errors -= 1
            raise RuntimeError("supplier down")

    async def issue(self, held, cost):
        self.calls.append(("issue", cost.amount_minor))
        if isinstance(self.issue_answer, Exception):
            raise self.issue_answer
        return self.issue_answer if self.issue_answer is not None else [f"98765432100{i}" for i in range(held.adults)]


ADULT_1 = {"type": "ADT", "title": "MR", "first_name": "Ali", "last_name": "Raza", "date_of_birth": "1988-03-14", "id_type": "PSPT",
           "document_number": "AB1234567", "document_country": "PAK", "document_expiry": "2031-09-30", "nationality": "PAK"}
ADULT_2 = {**ADULT_1, "title": "MRS", "first_name": "Sana", "date_of_birth": "1990-05-02", "document_number": "CD7654321"}


def request(passengers=None, adults=2, **contact):
    return GroupHoldRequest(ticket_id=UUID(TICKET_ID), adults=adults, passengers=passengers or [ADULT_1, ADULT_2], contact={
        "full_name": "Ali Raza", "phone": "+92 300 1234567", "email": "ali.raza@example.invalid", "id_type": "PSPT",
        "document_number": "AB1234567", **contact})


@pytest.fixture
def provider(database):
    connection, metadata, _ = database
    if not connection.execute(text("SELECT 1 FROM users WHERE id=:id"), {"id": ACTOR}).first():
        connection.execute(metadata.tables["users"].insert().values(id=ACTOR, cognito_issuer="https://t.invalid", cognito_sub="s"))
    fake = FakeProvider()
    with patch.dict(group_holds.PROVIDERS, {VENDOR: lambda: fake}), \
         patch.object(settings, "group_ticketing_markup_percent", Decimal("5")), patch.object(settings, "group_ticketing_markup_fixed_minor", 0):
        yield fake


def run(coro):
    return asyncio.run(coro)


def place(viewer=ADMIN, req=None, stock=None):
    return run(group_holds.hold(req or request(), viewer, fetch=AsyncMock(return_value=stock or inventory())))


def pay(reference, amount):
    account = next((a for a in payment_accounts.list_accounts() if a.active and a.method == "bank_transfer"), None) \
        or payment_accounts.add("bank_transfer", "Meezan Bank, Travelian current account")
    return booking_payments.record(reference, amount_minor=amount, currency="PKR", method="bank_transfer", account_id=account.id,
                                   payer_reference=f"TXN-{amount}", received_at=datetime.now(timezone.utc), actor_user_id=ACTOR)


def test_hold_returns_the_price_only_after_the_hold_and_keeps_supplier_ref_inside(provider, database):
    view = place()
    assert view.booking_id.startswith("TRV-GRP-") and view.pnr == "K7P2QX" and view.status == "held"
    # supplier 100,000 + 5% = 105,000 per adult; two adults.
    assert view.price_per_adult.amount_minor == 105_000_00 and view.total.amount_minor == 210_000_00
    assert view.amount_due.amount_minor == 210_000_00 and view.adults == 2 and view.stock == "supplier"
    assert (view.flight.airline, view.flight.sector, view.flight.departure_date, view.flight.departure_time) == ("Fly Jinnah", "ISB - JED", "2026-11-22", "06:40")
    assert [t.document_number_masked for t in view.travelers] == ["••••••567", "••••••321"]
    body = view.model_dump_json()
    for secret in (SUPPLIER_REF, "AB1234567", "1988-03-14"):
        assert secret not in body
    connection, _, _ = database
    event = connection.execute(text("SELECT action FROM booking_events e JOIN bookings b ON b.id = e.booking_id WHERE b.reference = :r"),
                               {"r": view.booking_id}).scalars().all()
    assert event == ["hold_created"]
    assert connection.scalar(text("SELECT product FROM bookings WHERE reference = :r"), {"r": view.booking_id}) == "group"


def test_supplier_name_only_for_viewers_allowed_to_see_it(provider):
    view = place()
    assert view.supplier == VENDOR
    assert view.pnr == "K7P2QX"
    staff_view = run(group_holds.read(view.booking_id, STAFF))
    assert staff_view.supplier is None and staff_view.pnr is None and staff_view.stock == "supplier"
    body = staff_view.model_dump_json()
    assert VENDOR not in body and "K7P2QX" not in body


def test_hold_failure_saves_nothing_and_is_never_retried(provider, database):
    provider.hold_error = RuntimeError("timeout")
    with pytest.raises(GroupHoldError) as error:
        place()
    assert error.value.code == "not_confirmed_hold" and error.value.status == 502
    assert provider.calls == [("hold", 2)]
    connection, _, _ = database
    assert connection.scalar(text("SELECT count(*) FROM bookings WHERE product = 'group'")) == 0


def test_supplier_refusal_and_unsupported_source(provider):
    provider.hold_error = SupplierRefused()
    with pytest.raises(GroupHoldError) as error:
        place()
    assert error.value.code == "not_held"
    with patch.dict(group_holds.PROVIDERS, {}, clear=True), pytest.raises(GroupHoldError) as error:
        place()
    assert error.value.code == "hold_not_supported"


def test_gender_must_match_the_title(provider):
    with pytest.raises(GroupHoldError) as error:
        place(req=request(passengers=[{**ADULT_1, "gender": "F"}, {**ADULT_2, "gender": "F"}]))
    assert error.value.code == "invalid_passengers" and error.value.field_errors[0][0] == ["passengers", 0, "gender"]
    assert provider.calls == []


@pytest.mark.parametrize("passengers, adults, code", [
    ([{**ADULT_1, "type": "CHD"}, ADULT_2], 2, "adults_only"),
    ([ADULT_1, {**ADULT_2, "date_of_birth": "2018-01-01"}], 2, "adults_only"),
    ([ADULT_1], 2, "passengers_mismatch"),
])
def test_adults_only(provider, passengers, adults, code):
    with pytest.raises(GroupHoldError) as error:
        place(req=request(passengers=passengers, adults=adults))
    assert error.value.code == code
    assert provider.calls == []


def test_adults_above_nine_are_refused_by_the_request():
    with pytest.raises(ValueError):
        request(passengers=[ADULT_1] * 10, adults=10)


def test_not_enough_seats_left(provider):
    with pytest.raises(GroupHoldError) as error:
        place(stock=inventory(remaining=1))
    assert error.value.code == "not_enough_seats" and provider.calls == []


def test_saved_customer_is_found_by_document_number_after_the_hold(provider):
    view = place()
    assert any(o.role == "customer" and o.status in ("saved", "linked") for o in view.saved_people)
    found = saved_people.search(None, "AB1234567")
    assert [(p.first_name, p.last_name) for p in found][:1] == [("Ali", "Raza")]


def test_issue_is_refused_until_nothing_is_due(provider):
    view = place()
    assert not view.can_issue_tickets
    pay(view.booking_id, 100_000_00)
    with pytest.raises(GroupHoldError) as error:
        run(group_holds.issue(view.booking_id, ADMIN))
    assert error.value.code == "payment_due" and "issue" not in str(provider.calls)
    pay(view.booking_id, 110_000_00)
    assert run(group_holds.read(view.booking_id, ADMIN)).can_issue_tickets


def test_issue_with_unchanged_price_issues_straight_away(provider):
    view = place()
    pay(view.booking_id, 210_000_00)
    result = run(group_holds.issue(view.booking_id, ADMIN))
    assert result.result == "issued" and result.hold.status == "issued"
    assert [t.ticket_number for t in result.hold.travelers] == ["987654321000", "987654321001"]
    assert provider.calls[-2:] == ["price", ("issue", 100_000_00)]


def test_price_changed_needs_agreement_then_issues_after_the_rest_is_paid(provider):
    view = place()
    pay(view.booking_id, 210_000_00)
    provider.cost = 103_000_00  # new selling 108,150 per adult -> 216,300
    check = run(group_holds.price_check(view.booking_id, ADMIN))
    assert check.changed and check.difference.amount_minor == 6_300_00
    with pytest.raises(GroupHoldError) as error:
        run(group_holds.issue(view.booking_id, ADMIN))
    assert error.value.code == "price_changed"
    assert error.value.extra["held_total"]["amount_minor"] == 210_000_00 and error.value.extra["new_total"]["amount_minor"] == 216_300_00
    assert ("issue", 103_000_00) not in provider.calls
    # Customer agrees: the total changes, and the difference must be paid first.
    with pytest.raises(GroupHoldError) as error:
        run(group_holds.issue(view.booking_id, ADMIN, GroupMoney(amount_minor=216_300_00, currency="PKR")))
    assert error.value.code == "payment_due"
    held = run(group_holds.read(view.booking_id, ADMIN))
    assert held.total.amount_minor == 216_300_00 and held.amount_due.amount_minor == 6_300_00
    pay(view.booking_id, 6_300_00)
    assert run(group_holds.issue(view.booking_id, ADMIN)).result == "issued"


def test_price_changed_and_customer_declines_releases_the_hold(provider, database):
    view = place()
    provider.cost = 99_000_00
    assert run(group_holds.price_check(view.booking_id, ADMIN)).difference.amount_minor < 0
    released = run(group_holds.release(view.booking_id, ADMIN))
    assert released.status == "released" and not released.can_record_payments and not released.can_issue_tickets
    assert ("release", SUPPLIER_REF) in provider.calls
    connection, _, _ = database
    actions = connection.execute(text("SELECT action FROM booking_events e JOIN bookings b ON b.id = e.booking_id WHERE b.reference = :r"
                                      " ORDER BY e.created_at"), {"r": view.booking_id}).scalars().all()
    assert actions == ["hold_created", "hold_released"]
    with pytest.raises(GroupHoldError) as error:
        run(group_holds.release(view.booking_id, ADMIN))
    assert error.value.code == "not_held"


def test_unclear_issue_is_unknown_and_never_retried(provider):
    view = place()
    pay(view.booking_id, 210_000_00)
    provider.issue_answer = RuntimeError("timeout")
    result = run(group_holds.issue(view.booking_id, ADMIN))
    assert result.result == "unknown" and result.hold.status == "unknown"
    assert [c for c in provider.calls if isinstance(c, tuple) and c[0] == "issue"] == [("issue", 100_000_00)]
    with pytest.raises(GroupHoldError):
        run(group_holds.issue(view.booking_id, ADMIN))


def test_expired_hold_disables_payment_and_issue(provider, database):
    view = place()
    connection, _, _ = database
    connection.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE reference = :r"), {"r": view.booking_id})
    with pytest.raises(booking_payments.PaymentRefused) as refused:
        pay(view.booking_id, 1_000_00)
    assert refused.value.status == 409
    expired = run(group_holds.read(view.booking_id, ADMIN))
    assert expired.status == "expired" and not expired.can_record_payments and not expired.can_issue_tickets
    for call in (group_holds.issue(view.booking_id, ADMIN), group_holds.release(view.booking_id, ADMIN),
                 group_holds.price_check(view.booking_id, ADMIN)):
        with pytest.raises(GroupHoldError) as error:
            run(call)
        assert error.value.code == "hold_expired"
    assert connection.scalar(text("SELECT booking_status FROM bookings WHERE reference = :r"), {"r": view.booking_id}) == "expired"


def test_staff_without_permission_cannot_issue(provider):
    view = place()
    with pytest.raises(GroupHoldError) as error:
        run(group_holds.issue(view.booking_id, STAFF))
    assert error.value.status == 403
