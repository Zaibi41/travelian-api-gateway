"""B2B agency wallets (decision D3: wallets exist for B2B agencies only; B2C customers pay per booking).

Each agency's balance is the sum of its append-only agency_wallet_entries: deposits (money the agency paid into a Travelian
account) and adjustments (+/- with a reason). A wallet never goes below zero. Migration 0038; before it, `available()` is
False and the routes say so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from app.db import agencies, finance
from app.db.supplier_ledger import _roles

Refused = finance.FinanceRefused
KINDS = ("deposit", "adjustment")


def available(connection=None) -> bool:
    def check(c) -> bool:
        return bool(c.scalar(text("SELECT to_regclass('agency_wallet_entries') IS NOT NULL")))
    if connection is not None:
        return check(connection)
    with agencies.application_engine().begin() as c:
        return check(c)


@dataclass
class Wallet:
    agency_id: str
    agency_name: str
    agency_status: str
    balance_minor: int
    deposits_minor: int
    last_deposit_at: datetime | None
    low_minor: int | None

    @property
    def low(self) -> bool | None:
        return None if self.low_minor is None else self.balance_minor < self.low_minor


_WALLETS = """
    SELECT a.id, a.name, a.status, a.wallet_low_minor,
           COALESCE(SUM(e.amount_minor), 0) AS balance,
           COALESCE(SUM(e.amount_minor) FILTER (WHERE e.kind = 'deposit'), 0) AS deposits,
           MAX(e.occurred_at) FILTER (WHERE e.kind = 'deposit') AS last_deposit
    FROM agencies a LEFT JOIN agency_wallet_entries e ON e.agency_id = a.id
"""


def _wallet(row) -> Wallet:
    return Wallet(row["id"], row["name"], row["status"], int(row["balance"]), int(row["deposits"]), row["last_deposit"], row["wallet_low_minor"])


def wallets() -> list[Wallet]:
    with agencies.application_engine().begin() as connection:
        _require(connection)
        rows = connection.execute(text(_WALLETS + " GROUP BY a.id ORDER BY a.name")).mappings().all()
    return [_wallet(r) for r in rows]


def wallet(connection, agency_id: str) -> Wallet:
    row = connection.execute(text(_WALLETS + " WHERE a.id = :a GROUP BY a.id"), {"a": agency_id}).mappings().one_or_none()
    if row is None:
        raise Refused(404, "Agency not found.", "not_found")
    return _wallet(row)


def _require(connection) -> None:
    if not available(connection):
        raise Refused(503, "Agency wallets need the latest Finance update on the server (migration 0038).", "not_migrated")


@dataclass
class Entry:
    at: datetime
    kind: str
    amount_minor: int
    balance_minor: int
    reference: str | None
    reason: str | None
    account: str | None
    by_role: str | None


def statement(agency_id: str) -> tuple[Wallet, list[Entry]]:
    with agencies.application_engine().begin() as connection:
        _require(connection)
        w = wallet(connection, agency_id)
        rows = connection.execute(text(
            "SELECT e.occurred_at, e.kind, e.amount_minor, e.reference, e.reason, p.label AS account, e.created_by_user_id"
            " FROM agency_wallet_entries e LEFT JOIN payment_accounts p ON p.id = e.account_id"
            " WHERE e.agency_id = :a ORDER BY e.occurred_at, e.created_at"), {"a": agency_id}).mappings().all()
        roles = _roles(connection, {r["created_by_user_id"] for r in rows if r["created_by_user_id"]})
    running, out = 0, []
    for r in rows:
        running += int(r["amount_minor"])
        out.append(Entry(r["occurred_at"], r["kind"], int(r["amount_minor"]), running, r["reference"], r["reason"], r["account"],
                         roles.get(r["created_by_user_id"]) if r["created_by_user_id"] else "system"))
    return w, out


def add_entry(actor: finance.Actor, agency_id: str, *, kind: str, amount_minor: int, reference: str | None, reason: str | None,
              occurred_at: datetime, account_id: str | None) -> Wallet:
    reference, reason = (reference or "").strip() or None, (reason or "").strip() or None
    if kind not in KINDS:
        raise Refused(422, "Choose deposit or adjustment.", "invalid_kind")
    if kind == "deposit" and amount_minor <= 0:
        raise Refused(422, "Enter the amount deposited.", "invalid_amount")
    if kind == "deposit" and not (reference and account_id):
        raise Refused(422, "Enter the transfer reference and the account the money arrived in.", "reference_required")
    if kind == "adjustment" and (amount_minor == 0 or not reason):
        raise Refused(422, "Enter the correction (+ or -) and why.", "reason_required")
    if occurred_at.tzinfo is None or occurred_at > datetime.now(timezone.utc):
        raise Refused(422, "Enter when it happened (not in the future).", "invalid_time")
    with agencies.application_engine().begin() as connection:
        _require(connection)
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"agency_wallet:{agency_id}"})
        before = wallet(connection, agency_id)
        if account_id and not connection.scalar(text("SELECT 1 FROM payment_accounts WHERE id = :id"), {"id": account_id}):
            raise Refused(422, "Choose the account from the list.", "unknown_account")
        if before.balance_minor + amount_minor < 0:
            raise Refused(409, f"That would take {before.agency_name}'s wallet below zero. Nothing was saved.", "negative_balance")
        entry_id = str(uuid.uuid4())
        connection.execute(text(
            "INSERT INTO agency_wallet_entries (id, agency_id, kind, amount_minor, reference, reason, account_id, occurred_at, created_by_user_id)"
            " VALUES (:id, :a, :k, :m, :ref, :why, :acc, :at, :u)"),
            {"id": entry_id, "a": agency_id, "k": kind, "m": amount_minor, "ref": reference, "why": reason, "acc": account_id,
             "at": occurred_at, "u": actor.user_id})
        finance.audit(connection, actor, f"agency_wallet_{kind}", "agency_wallet", agency_id,
                      before={"balance_minor": before.balance_minor}, after={"amount_minor": amount_minor, "reference": reference},
                      reason=reason)
        return wallet(connection, agency_id)


def set_low(actor: finance.Actor, agency_id: str, low_minor: int | None) -> Wallet:
    if low_minor is not None and low_minor < 0:
        raise Refused(422, "Enter an amount of zero or more, or leave it empty.", "invalid_amount")
    with agencies.application_engine().begin() as connection:
        _require(connection)
        before = wallet(connection, agency_id)
        connection.execute(text("UPDATE agencies SET wallet_low_minor = :m WHERE id = :a"), {"m": low_minor, "a": agency_id})
        finance.audit(connection, actor, "agency_wallet_low_set", "agency_wallet", agency_id,
                      before={"wallet_low_minor": before.low_minor}, after={"wallet_low_minor": low_minor})
        return wallet(connection, agency_id)
