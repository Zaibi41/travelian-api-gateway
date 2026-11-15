"""Group price re-check and issue: the selling price is never recalculated. Only the supplier cost can change; profit is
selling - cost. "Changed" means the supplier cost moved. A cost above the selling price needs an explicit confirmation.
The store is mocked; tests/database/test_group_holds.py covers the SQL (accept_cost keeps traveler costs in step)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.core.field_visibility import ALL, NONE, BackOfficeUser, resolve_actions
from app.db import group_holds as store
from app.db import issuance as issuance_store
from app.services import group_holds
from app.services.group_holds import GroupHoldError
from app.services.group_ticketing_view import GroupMoney

ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))
ISSUER = BackOfficeUser("u-3", "staff", NONE, resolve_actions("staff", {"issue_tickets"}))
SELLING, COST = 240_000_00, 200_000_00  # a staff-set selling price: 120,000 per adult, well above cost + markup


class Provider:
    def __init__(self, cost_each):
        self.cost_each, self.calls = cost_each, []

    async def price(self, held):
        self.calls.append("price")
        return GroupMoney(amount_minor=self.cost_each, currency="PKR")

    async def issue(self, held, cost):
        self.calls.append("issue")
        return ["111", "222"]

    async def hold(self, *a): ...
    async def release(self, held): ...


def booking(cost=COST, paid=SELLING):
    return store.GroupBooking("b-1", "TRV-GRP-AB12CD", "PNR1", "fake", "held", "not_issued", "paid" if paid >= SELLING else "unpaid",
                              datetime.now(timezone.utc) + timedelta(minutes=20), "PKR", SELLING, cost, "ref-1", [], 2, paid, {})


def run(coro, provider, record, accept=None):
    claim = MagicMock(side_effect=issuance_store.IssuanceRefused(409, "stop here"))
    with patch.dict(group_holds.PROVIDERS, {"fake": lambda: provider}), patch.object(group_holds, "supports_issue", lambda p: True), \
         patch.object(store, "expire_if_over", lambda reference, now, no_release=frozenset(): record), \
         patch.object(group_holds, "refresh_supplier", lambda b: _same(b)), \
         patch.object(store, "accept_cost", accept or MagicMock(return_value=record)), patch.object(issuance_store, "claim", claim):
        return asyncio.run(coro), claim


async def _same(value):
    return value


def test_unchanged_cost_is_not_a_change_even_with_a_staff_selling_price():
    check, _ = run(group_holds.price_check("TRV-GRP-AB12CD", ADMIN), Provider(100_000_00), booking())
    assert check.changed is False and check.below_cost is False
    assert check.selling_total.amount_minor == SELLING and check.profit.amount_minor == SELLING - COST


def test_moved_cost_is_a_change_selling_stays_profit_recomputed_cost_details_only_with_access():
    check, _ = run(group_holds.price_check("TRV-GRP-AB12CD", ADMIN), Provider(103_000_00), booking())
    assert check.changed and check.selling_total.amount_minor == SELLING
    assert check.new_supplier_cost.amount_minor == 206_000_00 and check.difference.amount_minor == 6_000_00
    assert check.profit.amount_minor == SELLING - 206_000_00
    staff, _ = run(group_holds.price_check("TRV-GRP-AB12CD", ISSUER), Provider(103_000_00), booking())
    assert staff.changed and staff.new_supplier_cost is None and staff.profit is None and staff.previous_supplier_cost is None


def test_issue_with_unchanged_cost_goes_straight_to_issuing_without_touching_prices():
    accept = MagicMock()
    with pytest.raises(GroupHoldError) as raised:
        run(group_holds.issue("TRV-GRP-AB12CD", ADMIN), Provider(100_000_00), booking(), accept)
    assert raised.value.message == "stop here" and accept.call_count == 0


def test_issue_with_moved_cost_needs_the_new_cost_confirmed_then_updates_only_the_cost():
    accept = MagicMock(return_value=booking(cost=206_000_00))
    with pytest.raises(GroupHoldError) as raised:
        run(group_holds.issue("TRV-GRP-AB12CD", ISSUER), Provider(103_000_00), booking(), accept)
    assert raised.value.code == "supplier_cost_changed" and accept.call_count == 0
    assert raised.value.extra["accepted_supplier_cost"] == {"amount_minor": 206_000_00, "currency": "PKR"}
    assert raised.value.extra["selling_total"]["amount_minor"] == SELLING
    assert "new_supplier_cost" not in raised.value.extra and "profit" not in raised.value.extra  # no cost access
    with pytest.raises(GroupHoldError) as raised:
        run(group_holds.issue("TRV-GRP-AB12CD", ISSUER, GroupMoney(amount_minor=206_000_00, currency="PKR")), Provider(103_000_00), booking(), accept)
    assert raised.value.message == "stop here"  # went on to issuing
    assert accept.call_args.args == ("TRV-GRP-AB12CD", "u-3", COST, 206_000_00)


def test_cost_above_selling_warns_and_needs_explicit_confirmation():
    accept = MagicMock(return_value=booking(cost=250_000_00))
    confirmed = GroupMoney(amount_minor=250_000_00, currency="PKR")
    with pytest.raises(GroupHoldError) as raised:
        run(group_holds.issue("TRV-GRP-AB12CD", ADMIN, confirmed), Provider(125_000_00), booking(), accept)
    assert raised.value.code == "below_cost" and raised.value.extra["below_cost"] is True and accept.call_count == 0
    assert raised.value.extra["profit"]["amount_minor"] == SELLING - 250_000_00
    with pytest.raises(GroupHoldError) as raised:
        run(group_holds.issue("TRV-GRP-AB12CD", ADMIN, confirmed, accept_below_cost=True), Provider(125_000_00), booking(), accept)
    assert raised.value.message == "stop here" and accept.call_args.args[-1] == 250_000_00


def test_a_cost_that_moved_again_is_refused():
    with pytest.raises(GroupHoldError) as raised:
        run(group_holds.issue("TRV-GRP-AB12CD", ADMIN, GroupMoney(amount_minor=206_000_00, currency="PKR")), Provider(104_000_00), booking())
    assert raised.value.code == "supplier_cost_changed"


def test_due_never_goes_negative_and_selling_stays_after_a_lower_cost():
    view = group_holds.build_view(booking(cost=150_000_00, paid=SELLING + 5_000_00), None, ADMIN, datetime.now(timezone.utc))
    assert view.amount_due.amount_minor == 0 and view.total.amount_minor == SELLING and view.profit.amount_minor == SELLING - 150_000_00
