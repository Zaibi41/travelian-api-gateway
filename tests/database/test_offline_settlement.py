"""travelian#202: a SooperFare booking settled and ticketed outside Travelian.

The real case: hold total Rs 76,006; the reseller paid Rs 75,000 by agreement; SooperFare billed Rs 74,886 and took it
from a refund credit Travelian already had with them; staff issued in SooperFare's portal.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.field_visibility import ALL, NONE, BackOfficeUser, FieldVisibility, resolve_actions
from app.db import booking_payments, finance, offline_settlement, payment_accounts, supplier_wallets
from app.db.issuance import IssuanceRefused
from app.db.offline_settlement import SettlementRefused, SupplierSettlement
from app.services import issuance
from tests.database.test_booking_people import ACTOR, save

ADMIN = BackOfficeUser(ACTOR, "super_admin", ALL, resolve_actions("super_admin", set()))
ISSUER_NO_COST = BackOfficeUser(ACTOR, "staff", FieldVisibility(False, False), frozenset({"issue_tickets"}))
VIEWER = BackOfficeUser(ACTOR, "staff", NONE)
WHO = finance.Actor(ACTOR, "super_admin")
TICKETS = {0: "2142345678901", 1: "2142345678902"}


@pytest.fixture
def sooperfare(database):
    """A SooperFare hold: total Rs 76,006 (hold-time supplier cost the same), Rs 75,000 received."""
    connection, metadata, _ = database
    reference = save(connection, metadata, provider="sooperfare", selling_total_minor=7_600_600, supplier_total_minor=7_600_600,
                     hold_expires_at=datetime.now(timezone.utc) + timedelta(hours=6))
    account = payment_accounts.add("bank_transfer", "Meezan Bank").id
    booking_payments.record(reference, amount_minor=7_500_000, currency="PKR", method="bank_transfer", account_id=account,
                            payer_reference="RESELLER-1", received_at=datetime.now(timezone.utc) - timedelta(hours=1), actor_user_id=ACTOR)
    # Travelian's SooperFare balance: a refund credit kept with them (Finance > Supplier balances).
    supplier_wallets.add_entry("sooperfare", WHO, kind="adjustment", amount_minor=10_000_000, reference=None,
                               reason="Refund credit kept with SooperFare", occurred_at=datetime.now(timezone.utc) - timedelta(days=1))
    return connection, reference


def booking(connection, reference):
    return connection.execute(text("SELECT * FROM bookings WHERE reference = :r"), {"r": reference}).mappings().one()


def record(reference, viewer=ADMIN, tickets=TICKETS, **kwargs):
    return asyncio.run(issuance.record_offline(reference, viewer, tickets, **kwargs))


def test_the_real_case_settles_issues_and_draws_the_credit(sooperfare):
    connection, reference = sooperfare
    target = issuance.store.load(reference)
    assert issuance.can_record_issue(target, ADMIN) and not issuance.can_record_issue(target, VIEWER)

    settled = offline_settlement.settle_due(reference, WHO, "Quoted Rs 75,000 to the reseller; agreed by the owner")
    assert (settled.waived_minor, settled.total_minor) == (100_600, 7_500_000)
    row = booking(connection, reference)
    assert (row["total_minor"], row["payment_status"], row["settled_original_total_minor"]) == (7_500_000, "paid", 7_600_600)

    outcome = record(reference, note="Issued in SooperFare's portal",
                     supplier=SupplierSettlement(billed_minor=7_488_600, paid_from="credit", reference="SF-CREDIT-77"))
    assert outcome.result == "issued" and outcome.tickets == TICKETS
    row = booking(connection, reference)
    assert (row["issuance_status"], row["booking_status"], row["supplier_paid_from"]) == ("issued", "ticket_issued", "credit")
    assert connection.execute(text("SELECT ticket_number FROM booking_travelers ORDER BY position")).scalars().all() == list(TICKETS.values())
    charges = connection.execute(text("SELECT amount_minor, source FROM supplier_charges")).all()
    assert [tuple(c) for c in charges] == [(7_488_600, "issuance")]  # the billed amount, once; not the hold-time cost
    assert supplier_wallets.wallet("sooperfare").balance_minor == 10_000_000 - 7_488_600  # the credit is drawn down

    view = offline_settlement.read(reference)
    assert (view.waived_minor, view.billed_minor, view.supplier_payment_reference) == (100_600, 7_488_600, "SF-CREDIT-77")
    assert view.issued_at is not None
    events = connection.execute(text("SELECT action FROM booking_events ORDER BY created_at, id")).scalars().all()
    assert "due_settled" in events and "issuance_recorded_offline" in events
    audit = connection.execute(text("SELECT action FROM finance_audit_log ORDER BY id")).scalars().all()
    assert {"due_settled", "supplier_charge_recorded", "supplier_payment_recorded"} <= set(audit)

    with pytest.raises(IssuanceRefused) as error:
        record(reference)
    assert error.value.status == 409


def test_fresh_bank_payment_leaves_the_supplier_balance_unchanged(sooperfare):
    _, reference = sooperfare
    record(reference, supplier=SupplierSettlement(billed_minor=7_488_600, paid_from="bank", reference="MEEZAN-TT-1"))
    assert supplier_wallets.wallet("sooperfare").balance_minor == 10_000_000


def test_without_a_billed_amount_the_hold_cost_is_the_charge(sooperfare):
    connection, reference = sooperfare
    record(reference, viewer=ISSUER_NO_COST)
    assert connection.execute(text("SELECT SUM(amount_minor) FROM supplier_charges")).scalar() == 7_600_600


def test_supplier_fields_need_the_cost_and_margin_permission(sooperfare):
    connection, reference = sooperfare
    with pytest.raises(IssuanceRefused) as error:
        record(reference, viewer=ISSUER_NO_COST, supplier=SupplierSettlement(billed_minor=7_488_600))
    assert error.value.status == 403
    assert booking(connection, reference)["issuance_status"] == "not_issued"


def test_needs_the_issue_permission_and_every_ticket(sooperfare):
    _, reference = sooperfare
    with pytest.raises(IssuanceRefused) as error:
        record(reference, viewer=VIEWER)
    assert error.value.status == 403
    with pytest.raises(IssuanceRefused) as error:
        record(reference, tickets={0: "2142345678901"})
    assert error.value.status == 422


def test_an_expired_hold_can_still_be_recorded_as_issued(sooperfare):
    connection, reference = sooperfare
    connection.execute(text("UPDATE bookings SET booking_status = 'paid_not_ticketed' WHERE reference = :r"), {"r": reference})
    assert record(reference).result == "issued"


def test_not_offered_when_tickets_are_issued_from_travelian(database):
    connection, metadata, _ = database
    reference = save(connection, metadata, hold_expires_at=datetime.now(timezone.utc) + timedelta(hours=6))
    from unittest.mock import patch

    from app.core.config import settings

    with patch.object(settings, "southair_ticketing_enabled", True):
        assert not issuance.can_record_issue(issuance.store.load(reference), ADMIN)
        with pytest.raises(IssuanceRefused) as error:
            record(reference)
    assert error.value.status == 409 and error.value.code == "issue_here"


def test_settle_needs_a_payment_a_due_and_a_reason(database):
    connection, metadata, _ = database
    reference = save(connection, metadata, provider="sooperfare", hold_expires_at=datetime.now(timezone.utc) + timedelta(hours=6))
    with pytest.raises(SettlementRefused) as error:
        offline_settlement.settle_due(reference, WHO, "Agreed with the reseller")
    assert error.value.status == 409 and error.value.code == "nothing_paid"
    with pytest.raises(SettlementRefused) as error:
        offline_settlement.settle_due(reference, WHO, "ok")
    assert error.value.status == 422
    booking_payments.record(reference, amount_minor=46_700, currency="PKR", method="bank_transfer",
                            account_id=payment_accounts.add("bank_transfer", "Meezan Bank").id, payer_reference="FULL",
                            received_at=datetime.now(timezone.utc) - timedelta(hours=1), actor_user_id=ACTOR)
    with pytest.raises(SettlementRefused) as error:
        offline_settlement.settle_due(reference, WHO, "Agreed with the reseller")
    assert error.value.code == "nothing_due"
