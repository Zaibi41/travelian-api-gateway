"""travelian#68: after a ticket is issued (Issue, or Check with supplier finding it issued) the supplier balance is
checked again: Southair is read again (read-only /my-profile) and kept as a snapshot, SooperFare's calculated balance is
checked against its alert level. A failure there never changes the issue result. Store, supplier and alerts are mocked."""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.field_visibility import ALL, BackOfficeUser, resolve_actions
from app.db import supplier_wallets as wallet_store
from app.services import issuance
from app.services import supplier_wallets as wallets

ADMIN = BackOfficeUser("u-1", "super_admin", ALL, resolve_actions("super_admin", set()))
ISSUED = issuance.Outcome("issued", "issued", "Ticket issued.", {1: "2141234567890"})
UNKNOWN = issuance.Outcome("unknown", "unknown", "The ticket's status is unknown.")


class FakeSouthair:
    def __init__(self, cap="98,500.00", error=None):
        self.cap, self.error, self.profiles = cap, error, 0

    async def profile(self):
        self.profiles += 1
        if self.error:
            raise self.error
        return {"limit": {"cap": self.cap}}


@pytest.fixture
def wired(monkeypatch):
    calls = {"snapshots": [], "checked": [], "provider": "southair"}
    monkeypatch.setattr(issuance.store, "load", lambda reference: SimpleNamespace(provider=calls["provider"]))
    monkeypatch.setattr(wallet_store, "record_snapshot", lambda provider, minor, currency, actor: calls["snapshots"].append((provider, minor, currency, actor.user_id)))

    async def check_low(provider):
        calls["checked"].append(provider)

    monkeypatch.setattr(wallets, "check_low", check_low)
    return calls


def run_issue(monkeypatch, outcome, southair, verify=False):
    async def fake(*args):
        return outcome

    monkeypatch.setattr(issuance, "_verify" if verify else "_issue", fake)
    call = issuance.verify if verify else issuance.issue
    return asyncio.run(call("TRV-1", ADMIN, client_factory=lambda: southair))


@pytest.mark.parametrize("verify", [False, True])
def test_southair_balance_is_read_again_and_kept_after_an_issue(monkeypatch, wired, verify):
    southair = FakeSouthair()
    assert run_issue(monkeypatch, ISSUED, southair, verify) is ISSUED
    assert southair.profiles == 1 and wired["snapshots"] == [("southair", 9_850_000, "PKR", "u-1")] and wired["checked"] == ["southair"]


def test_sooperfare_calculated_balance_is_checked_after_an_issue(monkeypatch, wired):
    wired["provider"] = "sooperfare"
    southair = FakeSouthair()
    assert run_issue(monkeypatch, ISSUED, southair) is ISSUED
    assert wired["checked"] == ["sooperfare"] and southair.profiles == 0 and wired["snapshots"] == []


def test_nothing_is_checked_when_the_ticket_isnt_issued(monkeypatch, wired):
    southair = FakeSouthair()
    assert run_issue(monkeypatch, UNKNOWN, southair) is UNKNOWN
    assert southair.profiles == 0 and wired["checked"] == [] and wired["snapshots"] == []


@pytest.mark.parametrize("break_it", ["southair_down", "no_balance", "store_fails", "alert_fails", "load_fails"])
def test_a_failed_balance_check_never_changes_the_issue_result(monkeypatch, wired, break_it):
    from app.vendors.flights.southair.client import SouthairError

    southair = FakeSouthair(error=SouthairError("timeout") if break_it == "southair_down" else None,
                            cap=None if break_it == "no_balance" else "98,500.00")
    if break_it == "store_fails":
        monkeypatch.setattr(wallet_store, "record_snapshot", lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    if break_it == "alert_fails":
        async def broken(provider):
            raise RuntimeError("notifications down")
        monkeypatch.setattr(wallets, "check_low", broken)
    if break_it == "load_fails":
        monkeypatch.setattr(issuance.store, "load", lambda reference: (_ for _ in ()).throw(RuntimeError("db down")))
    assert run_issue(monkeypatch, ISSUED, southair) is ISSUED
