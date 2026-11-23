"""Issuing workflow against a fake supplier: success, error then PNR decides, unknown, expiry, double clicks."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.field_visibility import ALL, NONE, BackOfficeUser, resolve_actions
from app.db import payment_accounts
from app.db import booking_payments
from app.db.issuance import IssuanceRefused
from app.services import issuance
from app.vendors.flights.southair.client import SouthairError
from tests.database.test_booking_people import ACTOR, save
from tests.southair.fixtures import TICKETING_OK, pnr_info, ticketed_pnr_info

APPROVER = BackOfficeUser(ACTOR, "super_admin", ALL, resolve_actions("super_admin", set()))
STAFF = BackOfficeUser(ACTOR, "staff", NONE)
ISSUED = pnr_info(status="TK", tickets=("2147483647001", "2147483647002"), paid=457.70, can_ticketing=False)


class FakeSouthair:
    def __init__(self, pnr_answers, ticketing=TICKETING_OK, balance="100000.00", gate=None):
        self.pnr_answers = list(pnr_answers)
        self.ticketing_answer = ticketing
        self.balance = balance
        self.gate = gate
        self.calls = []

    async def pnr_info(self, pnr):
        self.calls.append("pnr_info")
        answer = self.pnr_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def profile(self):
        self.calls.append("profile")
        return {"office": {"currency": "PKR"}, "limit": {"cap": self.balance, "ccy": "PKR"}}

    async def ticketing(self, *, ticket_ref, cash_amount):
        self.calls.append(("ticketing", ticket_ref, cash_amount))
        if self.gate:
            await self.gate.wait()
        if isinstance(self.ticketing_answer, Exception):
            raise self.ticketing_answer
        return self.ticketing_answer


@pytest.fixture
def held(database):
    """A UAT hold (1 adult + 1 infant, total Rs 467, supplier Rs 457.70), fully paid, ticketing on."""
    connection, metadata, _ = database
    reference = save(connection, metadata)
    booking_payments.record(reference, amount_minor=46_700, currency="PKR", method="bank_transfer",
                            account_id=payment_accounts.add("bank_transfer", "Meezan Bank").id,
                            payer_reference="TXN-1", received_at=datetime(2026, 11, 9, tzinfo=timezone.utc), actor_user_id=ACTOR)
    with patch.object(settings, "southair_ticketing_enabled", True):
        yield connection, reference


def status(connection, reference):
    return connection.execute(text("SELECT issuance_status, booking_status FROM bookings WHERE reference=:r"), {"r": reference}).one()


def actions(connection):
    return connection.execute(text("SELECT action FROM booking_events WHERE action LIKE 'issuance%' ORDER BY created_at, id")).scalars().all()


def tickets(connection):
    return connection.execute(text("SELECT ticket_number FROM booking_travelers ORDER BY position")).scalars().all()


def issue(reference, fake, viewer=APPROVER):
    return asyncio.run(issuance.issue(reference, viewer, client_factory=lambda: fake))


def test_success_sends_cash_once_and_stores_ticket_numbers_per_traveler(held):
    connection, reference = held
    fake = FakeSouthair([pnr_info(), ISSUED])
    outcome = issue(reference, fake)
    assert outcome.result == "issued" and outcome.tickets == {0: "2147483647001", 1: "2147483647002"}
    assert [c for c in fake.calls if c[0] == "ticketing"] == [("ticketing", "REF", "457.7")]  # the exact supplier total, once
    assert tuple(status(connection, reference)) == ("issued", "ticket_issued")
    assert tickets(connection) == ["2147483647001", "2147483647002"]
    assert actions(connection) == ["issuance_started", "issuance_issued"]
    with pytest.raises(IssuanceRefused) as error:
        issue(reference, FakeSouthair([]))
    assert error.value.status == 409 and "already issued" in error.value.message


@pytest.mark.parametrize("failure", [SouthairError("timeout", "x"), SouthairError("ticketing_failed", "x"), RuntimeError("boom")])
def test_error_then_pnr_confirms_issued(held, failure):
    connection, reference = held
    fake = FakeSouthair([pnr_info(), ISSUED], ticketing=failure)
    assert issue(reference, fake).result == "issued"
    assert status(connection, reference).issuance_status == "issued" and tickets(connection)[0] == "2147483647001"
    assert sum(1 for c in fake.calls if c[0] == "ticketing") == 1


def test_error_then_pnr_shows_not_issued_returns_to_issuable(held):
    connection, reference = held
    fake = FakeSouthair([pnr_info(), pnr_info()], ticketing=SouthairError("ticketing_failed", "x"))
    outcome = issue(reference, fake)
    assert outcome.result == "not_issued" and status(connection, reference).issuance_status == "not_issued"
    assert actions(connection) == ["issuance_started", "issuance_failed"]
    assert issue(reference, FakeSouthair([pnr_info(), ISSUED])).result == "issued"  # a later attempt is allowed


@pytest.mark.parametrize("after", [SouthairError("timeout", "x"), pnr_info(tickets=("2147483647001", None), paid=457.70), pnr_info(paid=457.70)])
def test_unresolved_becomes_unknown_and_blocks_issuing_until_resolved(held, after):
    connection, reference = held
    fake = FakeSouthair([pnr_info(), after], ticketing=SouthairError("timeout", "x"))
    assert issue(reference, fake).result == "unknown"
    assert status(connection, reference).issuance_status == "unknown"
    with pytest.raises(IssuanceRefused) as error:
        issue(reference, FakeSouthair([]))
    assert error.value.status == 409 and "unknown" in error.value.message
    # Verify: still unclear -> stays unknown; resolve needs a ticket number for every traveler.
    assert asyncio.run(issuance.verify(reference, APPROVER, client_factory=lambda: FakeSouthair([after if not isinstance(after, Exception) else pnr_info(paid=1)]))).result == "unknown"
    with pytest.raises(IssuanceRefused) as error:
        asyncio.run(issuance.resolve(reference, APPROVER, "issued", "Confirmed by phone with the supplier", {0: "2147483647001"}))
    assert error.value.status == 422
    outcome = asyncio.run(issuance.resolve(reference, APPROVER, "issued", "Confirmed by phone with the supplier", {0: "2147483647001", 1: "2147483647002"}))
    assert outcome.result == "issued" and tickets(connection) == ["2147483647001", "2147483647002"]
    assert actions(connection)[-1] == "issuance_resolved"


def test_unknown_can_be_verified_from_the_supplier(held):
    connection, reference = held
    issue(reference, FakeSouthair([pnr_info(), SouthairError("timeout", "x")], ticketing=SouthairError("timeout", "x")))
    assert asyncio.run(issuance.verify(reference, APPROVER, client_factory=lambda: FakeSouthair([pnr_info()]))).result == "not_issued"
    assert status(connection, reference).issuance_status == "not_issued"


def test_ticket_number_in_sale_is_issued_not_unknown(held):
    """UAT 19 Nov 2026 (TRV-ZL4HUF): ticketing OK, then pnr_info with the number only in names[].sale.tkt_no."""
    connection, reference = held
    after = ticketed_pnr_info(pnr="63B55C", tickets=("9000000000672", "9000000000673"), total=457.70)
    assert issue(reference, FakeSouthair([pnr_info(), after])).result == "issued"
    assert tickets(connection) == ["9000000000672", "9000000000673"]


def test_unknown_from_the_old_reading_is_settled_by_check_with_supplier(held):
    connection, reference = held
    issue(reference, FakeSouthair([pnr_info(), pnr_info(status="OK", paid=457.70)]))  # what the old reading saw
    assert status(connection, reference).issuance_status == "unknown"
    checklist_attempt = issuance.describe("unknown", (actions(connection)[-1], {"reason_code": "unclear_after_ticketing",
                                                                             "supplier_answer": "ok", "pnr_status": "OK"}, datetime.now(timezone.utc)))
    assert checklist_attempt.can_check and "didn't show a ticket number" in checklist_attempt.reason and "accepted the request" in checklist_attempt.reason
    after = ticketed_pnr_info(pnr="63B55C", tickets=("9000000000672", "9000000000673"), total=457.70)
    fake = FakeSouthair([after])
    assert asyncio.run(issuance.verify(reference, APPROVER, client_factory=lambda: fake)).result == "issued"
    assert fake.calls == ["pnr_info", "profile"]  # read-only: the PNR, then the balance again once issued; never a ticketing call
    assert tickets(connection) == ["9000000000672", "9000000000673"] and actions(connection)[-1] == "issuance_verified"


def test_check_with_supplier_after_a_failed_attempt(held):
    connection, reference = held
    with pytest.raises(IssuanceRefused):
        issue(reference, FakeSouthair([pnr_info()], balance="1.00"))  # aborted: insufficient balance, nothing sent
    last = issuance.describe("not_issued", ("issuance_aborted", {"reason_code": "insufficient_balance"}, datetime.now(timezone.utc)))
    assert last.can_check and last.outcome == "aborted" and "Not enough balance" in last.reason
    fake = FakeSouthair([pnr_info()])
    outcome = asyncio.run(issuance.verify(reference, APPROVER, client_factory=lambda: fake))
    assert (outcome.result, outcome.issuance_status) == ("not_issued", "not_issued") and "safe to issue again" in outcome.message
    assert fake.calls == ["pnr_info"] and actions(connection)[-1] == "issuance_checked"
    # After a check, nothing more to check (it isn't a failed attempt any more); unclear on a check blocks issuing.
    with pytest.raises(IssuanceRefused):
        asyncio.run(issuance.verify(reference, APPROVER, client_factory=lambda: FakeSouthair([pnr_info()])))
    with pytest.raises(IssuanceRefused):
        issue(reference, FakeSouthair([pnr_info()], balance="1.00"))
    assert asyncio.run(issuance.verify(reference, APPROVER, client_factory=lambda: FakeSouthair([pnr_info(paid=10)]))).result == "unknown"
    assert status(connection, reference).issuance_status == "unknown"


def test_check_with_supplier_is_refused_without_a_failed_or_unknown_attempt(held):
    _, reference = held
    with pytest.raises(IssuanceRefused) as error:
        asyncio.run(issuance.verify(reference, APPROVER, client_factory=lambda: FakeSouthair([])))
    assert error.value.status == 409


def test_resolve_not_issued_needs_a_reason_via_the_api_and_reopens_issuing(held):
    connection, reference = held
    issue(reference, FakeSouthair([pnr_info(), SouthairError("timeout", "x")], ticketing=SouthairError("timeout", "x")))
    assert asyncio.run(issuance.resolve(reference, APPROVER, "not_issued", "Supplier confirmed no ticket", {})).result == "not_issued"
    assert status(connection, reference).issuance_status == "not_issued"


def test_expired_hold_is_refused_before_any_supplier_call(held):
    connection, reference = held
    connection.execute(text("UPDATE bookings SET hold_expires_at = now() + interval '30 seconds' WHERE reference=:r"), {"r": reference})
    fake = FakeSouthair([])
    with pytest.raises(IssuanceRefused) as error:
        issue(reference, fake)
    assert error.value.status == 409 and "expired" in error.value.message and fake.calls == []
    assert status(connection, reference).issuance_status == "not_issued" and actions(connection) == []


def test_no_double_issue_under_concurrent_clicks(held):
    connection, reference = held

    async def both():
        gate = asyncio.Event()
        fake = FakeSouthair([pnr_info(), ISSUED], gate=gate)
        first = asyncio.create_task(issuance.issue(reference, APPROVER, client_factory=lambda: fake))
        while not any(c[0] == "ticketing" for c in fake.calls):
            await asyncio.sleep(0.01)  # the first click is now inside the ticketing call
        with pytest.raises(IssuanceRefused) as error:
            await issuance.issue(reference, APPROVER, client_factory=lambda: FakeSouthair([pnr_info(), ISSUED]))
        assert error.value.status == 409 and "in progress" in error.value.message
        gate.set()
        return await first, fake

    outcome, fake = asyncio.run(both())
    assert outcome.result == "issued" and sum(1 for c in fake.calls if c[0] == "ticketing") == 1
    assert actions(connection) == ["issuance_started", "issuance_issued"]


@pytest.mark.parametrize("before,balance,code", [
    (pnr_info(total=500.00), "100000.00", "below_cost"),  # the airline's total is above the Rs 467 selling price (pricing, 0021)
    (pnr_info(can_ticketing=False), "100000.00", "not_ticketable"),
    (pnr_info(), "0.00", "insufficient_balance"),
    (SouthairError("timeout", "x"), "100000.00", "pnr_check_failed"),
])
def test_checks_before_sending_abort_without_ticketing(held, before, balance, code):
    connection, reference = held
    fake = FakeSouthair([before], balance=balance)
    with pytest.raises(IssuanceRefused) as error:
        issue(reference, fake)
    assert "Nothing was issued" in error.value.message and not any(c[0] == "ticketing" for c in fake.calls)
    assert status(connection, reference).issuance_status == "not_issued"
    detail = connection.execute(text("SELECT details->>'reason_code' FROM booking_events WHERE action='issuance_aborted'")).scalar()
    assert detail == code


def test_unclear_pnr_before_sending_becomes_unknown_without_ticketing(held):
    connection, reference = held
    fake = FakeSouthair([pnr_info(paid=457.70)])  # money paid, no tickets
    assert issue(reference, fake).result == "unknown"
    assert not any(c[0] == "ticketing" for c in fake.calls)


def test_ticketing_flag_off_and_permissions(held):
    connection, reference = held
    with patch.object(settings, "southair_ticketing_enabled", False), pytest.raises(IssuanceRefused) as error:
        issue(reference, FakeSouthair([]))
    assert error.value.status == 409 and "switched off" in error.value.message
    with pytest.raises(IssuanceRefused) as error:
        issue(reference, FakeSouthair([]), viewer=STAFF)
    assert error.value.status == 403
    # Staff without the permission can request issuance; it needs full payment and a valid hold.
    assert asyncio.run(issuance.request(reference, STAFF, "Customer paid in full")).result == "requested"
    assert status(connection, reference).issuance_status == "requested"
    assert issue(reference, FakeSouthair([pnr_info(), ISSUED])).result == "issued"


def test_request_needs_full_payment(database):
    connection, metadata, _ = database
    reference = save(connection, metadata)
    with pytest.raises(IssuanceRefused) as error:
        asyncio.run(issuance.request(reference, STAFF))
    assert error.value.status == 409 and "Full payment" in error.value.message
