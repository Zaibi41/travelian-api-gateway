"""Double-entry ledger (migration 0032, travelian#72). Integer paisa; every journal balances (database-checked).

Automatic journals, one per source record, posted by `post_pending` (scheduled, on demand, and as the backfill):

  payment confirmed   Dr bank / PayFast clearing (fare + card fee)   Cr customer deposits (fare), Cr card fee income
  ticket issued       Dr customer deposits (selling)                 Cr ticket sales
  supplier charge     Dr cost of tickets                             Cr supplier account      (negative: reversed)
  payment fee         Dr payment processing fees                     Cr the payment's bank / PayFast clearing
  refund paid         Dr customer deposits (not ticketed) or refunds Cr PayFast clearing (card) or bank (general)
  supplier top-up     Dr supplier account                            Cr bank (general)
  wallet adjustment   Dr supplier account / supplier adjustments     (by sign)

Test bookings and imported sample rows are never posted; bookings whose data can't post fully are listed as gaps.
Manual journals: made by one Finance user, posted only when another approves (maker/checker).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.db import agencies, finance

PKT = ZoneInfo("Asia/Karachi")
TYPES = ("asset", "liability", "equity", "income", "expense")
DEBIT_NORMAL = ("asset", "expense")


class LedgerRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _day(value: datetime | date | None) -> date:
    if value is None:
        return datetime.now(PKT).date()
    return value.astimezone(PKT).date() if isinstance(value, datetime) else value


def _code(connection, key: str) -> str:
    code = connection.scalar(text("SELECT code FROM ledger_accounts WHERE system_key = :k"), {"k": key})
    if code is None:
        raise LedgerRefused(500, f"The ledger account for {key} is missing.")
    return code


def bank_code(connection, payment_account_id: str | None) -> str:
    """The receiving account's own bank ledger account (created the first time it's needed), else bank (general)."""
    if not payment_account_id:
        return _code(connection, "bank")
    code = connection.scalar(text("SELECT code FROM ledger_accounts WHERE payment_account_id = :a"), {"a": payment_account_id})
    if code:
        return code
    label = connection.scalar(text("SELECT label FROM payment_accounts WHERE id = :a"), {"a": payment_account_id}) or "Receiving account"
    taken = {int(c) for c in connection.scalars(text("SELECT code FROM ledger_accounts WHERE code ~ '^11[0-4][0-9]$'"))}
    number = next(n for n in range(1101, 1150) if n not in taken)
    connection.execute(text("INSERT INTO ledger_accounts (code, name, type, payment_account_id) VALUES (:c, :n, 'asset', :a)"),
                       {"c": str(number), "n": label[:120], "a": payment_account_id})
    return str(number)


def supplier_code(connection, provider_id: str | None) -> str:
    code = connection.scalar(text("SELECT code FROM ledger_accounts WHERE system_key = :k"), {"k": f"supplier:{provider_id}"})
    return code or _code(connection, "supplier:other")


Line = tuple[str, int, int, str | None]  # (account code, debit, credit, memo)


def _post(connection, *, source_type: str, source_id: str | None, posted_on: date, description: str, lines: list[Line],
          booking_id: str | None = None, actor: finance.Actor | None = None, status: str = "posted") -> str:
    lines = [line for line in lines if line[1] or line[2]]
    if not lines:
        raise LedgerRefused(422, "A journal needs at least one amount.")
    if sum(l[1] for l in lines) != sum(l[2] for l in lines):
        raise LedgerRefused(422, "Debits and credits must be equal.")
    journal_id = str(uuid4())
    connection.execute(text(
        "INSERT INTO journals (id, posted_on, description, source_type, source_id, booking_id, status, created_by_user_id)"
        " VALUES (:id, :d, :desc, :t, :s, :b, :st, :u)"),
        {"id": journal_id, "d": posted_on, "desc": description[:300], "t": source_type, "s": source_id, "b": booking_id, "st": status,
         "u": actor.user_id if actor else None})
    for code, debit, credit, memo in lines:
        connection.execute(text("INSERT INTO journal_lines (journal_id, account_code, debit_minor, credit_minor, memo) VALUES (:j, :a, :d, :c, :m)"),
                           {"j": journal_id, "a": code, "d": debit, "c": credit, "m": (memo or "")[:200] or None})
    connection.execute(text("SET CONSTRAINTS journal_lines_balanced IMMEDIATE"))  # checked now, not only at commit
    connection.execute(text("SET CONSTRAINTS journal_lines_balanced DEFERRED"))   # the next journal's lines are checked when complete
    return journal_id


def _signed(code_debit: str, code_credit: str, amount: int, memo: str | None = None) -> list[Line]:
    """A positive amount debits the first account; a negative one reverses the sides."""
    if amount >= 0:
        return [(code_debit, amount, 0, memo), (code_credit, 0, amount, memo)]
    return [(code_credit, -amount, 0, memo), (code_debit, 0, -amount, memo)]


def _gap(connection, source_type: str, source_id: str, booking_id: str | None, reason: str, detail: str) -> int:
    row = connection.execute(text("INSERT INTO ledger_gaps (source_type, source_id, booking_id, reason, detail) VALUES (:t, :s, :b, :r, :d)"
                                  " ON CONFLICT DO NOTHING RETURNING id"), {"t": source_type, "s": source_id, "b": booking_id, "r": reason, "d": detail}).first()
    return 1 if row else 0


EXCLUDED = "(b.is_test OR b.import_source IS NOT NULL)"
NOT_POSTED = "NOT EXISTS (SELECT 1 FROM journals j WHERE j.source_type = '{t}' AND j.source_id = {id})"


@dataclass
class PostResult:
    journals: int = 0
    gaps: int = 0


def post_pending(actor: finance.Actor = finance.SYSTEM) -> PostResult:
    """Post every source record that has no journal yet (idempotent: unique per source). Also the backfill."""
    result = PostResult()
    with agencies.application_engine().begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('ledger_post'))"))
        sales, deposits = _code(connection, "sales"), _code(connection, "customer_deposits")
        card_income, clearing = _code(connection, "card_fee_income"), _code(connection, "payfast_clearing")

        for p in connection.execute(text(
                "SELECT p.id, p.booking_id, b.reference, p.method, p.account_id, p.amount_minor, p.card_fee_minor, p.received_at, "
                f"{EXCLUDED} AS excluded FROM booking_payments p JOIN bookings b ON b.id = p.booking_id WHERE p.currency = 'PKR' AND "
                + NOT_POSTED.format(t="payment", id="p.id"))).mappings().all():
            if p["excluded"]:
                result.gaps += _gap(connection, "payment", p["id"], p["booking_id"], "test_or_sample", f"{p['reference']}: test or sample booking, not posted.")
                continue
            cash = clearing if p["method"] == "payfast" else bank_code(connection, p["account_id"])
            fee = p["card_fee_minor"] or 0
            _post(connection, source_type="payment", source_id=p["id"], posted_on=_day(p["received_at"]), booking_id=p["booking_id"],
                  description=f"Payment received {p['reference']}", lines=[
                      (cash, p["amount_minor"] + fee, 0, p["method"]), (deposits, 0, p["amount_minor"], None), (card_income, 0, fee, "card fee")])
            result.journals += 1

        for b in connection.execute(text(
                "SELECT b.id, b.reference, b.total_minor, b.cost_known, b.updated_at, " + EXCLUDED + " AS excluded,"
                " (SELECT max(e.created_at) FROM booking_events e WHERE e.booking_id = b.id AND e.action IN"
                "  ('issuance_issued', 'issuance_found_issued', 'issuance_verified', 'issuance_resolved', 'group_ticketed')) AS issued_at,"
                " EXISTS (SELECT 1 FROM supplier_charges s WHERE s.booking_id = b.id) AS has_charge"
                " FROM bookings b WHERE b.booking_status = 'ticket_issued' AND b.currency = 'PKR' AND " + NOT_POSTED.format(t="ticket", id="b.id"))
                ).mappings().all():
            if b["excluded"]:
                result.gaps += _gap(connection, "ticket", b["id"], b["id"], "test_or_sample", f"{b['reference']}: test or sample booking, not posted.")
                continue
            _post(connection, source_type="ticket", source_id=b["id"], posted_on=_day(b["issued_at"] or b["updated_at"]), booking_id=b["id"],
                  description=f"Ticket issued {b['reference']}", lines=[(deposits, b["total_minor"], 0, None), (sales, 0, b["total_minor"], None)])
            result.journals += 1
            if not b["has_charge"]:
                result.gaps += _gap(connection, "ticket", b["id"], b["id"], "no_supplier_charge" if b["cost_known"] else "cost_not_known",
                                    f"{b['reference']}: ticketed with no supplier charge recorded"
                                    + ("" if b["cost_known"] else " and its cost isn't known") + "; cost of tickets is missing until it's recorded.")

        cost = _code(connection, "cost_of_sales")
        for s in connection.execute(text(
                "SELECT s.id, s.booking_id, b.reference, s.provider_id, s.amount_minor, s.recorded_at, " + EXCLUDED + " AS excluded"
                " FROM supplier_charges s JOIN bookings b ON b.id = s.booking_id WHERE s.currency = 'PKR' AND "
                + NOT_POSTED.format(t="supplier_charge", id="s.id"))).mappings().all():
            if s["excluded"]:
                continue
            _post(connection, source_type="supplier_charge", source_id=s["id"], posted_on=_day(s["recorded_at"]), booking_id=s["booking_id"],
                  description=f"Supplier charge {s['reference']}", lines=_signed(cost, supplier_code(connection, s["provider_id"]), s["amount_minor"]))
            result.journals += 1

        fees = _code(connection, "payment_fees")
        for f in connection.execute(text(
                "SELECT f.id, p.booking_id, b.reference, p.method, p.account_id, f.amount_minor, f.recorded_at, " + EXCLUDED + " AS excluded"
                " FROM payment_fees f JOIN booking_payments p ON p.id = f.payment_id JOIN bookings b ON b.id = p.booking_id WHERE f.currency = 'PKR' AND "
                + NOT_POSTED.format(t="payment_fee", id="f.id"))).mappings().all():
            if f["excluded"]:
                continue
            cash = clearing if f["method"] == "payfast" else bank_code(connection, f["account_id"])
            _post(connection, source_type="payment_fee", source_id=f["id"], posted_on=_day(f["recorded_at"]), booking_id=f["booking_id"],
                  description=f"Payment fee {f['reference']}", lines=_signed(fees, cash, f["amount_minor"]))
            result.journals += 1

        refunds = _code(connection, "refunds")
        for r in connection.execute(text(
                "SELECT r.id, r.booking_id, b.reference, r.method, r.amount_minor, r.paid_at, " + EXCLUDED + " AS excluded,"
                " EXISTS (SELECT 1 FROM journals j WHERE j.source_type = 'ticket' AND j.source_id = r.booking_id) AS ticketed"
                " FROM refunds r JOIN bookings b ON b.id = r.booking_id WHERE r.status = 'paid' AND r.currency = 'PKR' AND "
                + NOT_POSTED.format(t="refund", id="r.id"))).mappings().all():
            if r["excluded"]:
                continue
            cash = clearing if r["method"] == "payfast" else _code(connection, "bank")
            _post(connection, source_type="refund", source_id=r["id"], posted_on=_day(r["paid_at"]), booking_id=r["booking_id"],
                  description=f"Refund paid {r['reference']}", lines=[(refunds if r["ticketed"] else deposits, r["amount_minor"], 0, None),
                                                                     (cash, 0, r["amount_minor"], r["method"])])
            result.journals += 1

        adjustments, bank = _code(connection, "wallet_adjustments"), _code(connection, "bank")
        for w in connection.execute(text(
                "SELECT w.id, w.provider_id, w.kind, w.amount_minor, w.occurred_at, w.reference FROM supplier_wallet_entries w WHERE w.currency = 'PKR' AND "
                "NOT EXISTS (SELECT 1 FROM journals j WHERE j.source_type IN ('top_up', 'adjustment') AND j.source_id = w.id)")).mappings().all():
            supplier = supplier_code(connection, w["provider_id"])
            if w["kind"] == "top_up":
                _post(connection, source_type="top_up", source_id=w["id"], posted_on=_day(w["occurred_at"]),
                      description=f"Supplier top-up {w['provider_id']} {w['reference'] or ''}".strip(), lines=_signed(supplier, bank, w["amount_minor"]))
            else:
                _post(connection, source_type="adjustment", source_id=w["id"], posted_on=_day(w["occurred_at"]),
                      description=f"Supplier balance adjustment {w['provider_id']}", lines=_signed(supplier, adjustments, w["amount_minor"]))
            result.journals += 1
        if result.journals or result.gaps:
            finance.audit(connection, actor, "ledger_posted", "ledger", None, after={"journals": result.journals, "gaps": result.gaps})
    return result


# ---------------------------------------------------------------------------------------------------- chart of accounts
def accounts(include_inactive: bool = True) -> list[dict]:
    with agencies.application_engine().begin() as connection:
        return [dict(r) for r in connection.execute(text(
            "SELECT code, name, type, system_key, payment_account_id, active FROM ledger_accounts"
            + ("" if include_inactive else " WHERE active") + " ORDER BY code")).mappings().all()]


def add_account(code: str, name: str, type_: str, actor: finance.Actor) -> dict:
    if type_ not in TYPES:
        raise LedgerRefused(422, "Choose asset, liability, equity, income or expense.")
    if not code.isdigit() or not 4 <= len(code) <= 10:
        raise LedgerRefused(422, "The code is 4 to 10 digits.")
    if not name.strip():
        raise LedgerRefused(422, "Enter the account name.")
    with agencies.application_engine().begin() as connection:
        if connection.scalar(text("SELECT 1 FROM ledger_accounts WHERE code = :c"), {"c": code}):
            raise LedgerRefused(409, "An account with this code exists.")
        connection.execute(text("INSERT INTO ledger_accounts (code, name, type) VALUES (:c, :n, :t)"), {"c": code, "n": name.strip()[:120], "t": type_})
        finance.audit(connection, actor, "ledger_account_added", "ledger_account", code, after={"name": name.strip(), "type": type_})
    return next(a for a in accounts() if a["code"] == code)


def update_account(code: str, actor: finance.Actor, *, name: str | None = None, active: bool | None = None) -> dict:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("SELECT * FROM ledger_accounts WHERE code = :c FOR UPDATE"), {"c": code}).mappings().one_or_none()
        if row is None:
            raise LedgerRefused(404, "Account not found.")
        if active is False and (row["system_key"] or row["payment_account_id"]):
            raise LedgerRefused(409, "Automatic journals post to this account, so it stays active. You can rename it.")
        if name is not None and not name.strip():
            raise LedgerRefused(422, "Enter the account name.")
        new = {"name": name.strip()[:120] if name is not None else row["name"], "active": row["active"] if active is None else active}
        connection.execute(text("UPDATE ledger_accounts SET name = :n, active = :a WHERE code = :c"), {"n": new["name"], "a": new["active"], "c": code})
        finance.audit(connection, actor, "ledger_account_changed", "ledger_account", code, before={"name": row["name"], "active": row["active"]}, after=new)
    return next(a for a in accounts() if a["code"] == code)


# ---------------------------------------------------------------------------------------------------- manual journals
def create_manual(actor: finance.Actor, posted_on: date, description: str, lines: list[Line]) -> str:
    if not description.strip():
        raise LedgerRefused(422, "Say what this journal is for.")
    if len(lines) < 2:
        raise LedgerRefused(422, "A journal needs at least two lines.")
    with agencies.application_engine().begin() as connection:
        active = set(connection.scalars(text("SELECT code FROM ledger_accounts WHERE active")))
        if any(code not in active for code, *_ in lines):
            raise LedgerRefused(422, "Use active accounts only.")
        journal_id = _post(connection, source_type="manual", source_id=None, posted_on=posted_on, description=description.strip(), lines=lines,
                           actor=actor, status="pending_approval")
        finance.audit(connection, actor, "manual_journal_created", "journal", journal_id,
                      after={"posted_on": posted_on, "description": description.strip(), "total_minor": sum(l[1] for l in lines)})
        return journal_id


def decide_manual(journal_id: str, actor: finance.Actor, approve: bool, reason: str | None = None) -> None:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("SELECT * FROM journals WHERE id = :id FOR UPDATE"), {"id": journal_id}).mappings().one_or_none()
        if row is None or row["source_type"] != "manual":
            raise LedgerRefused(404, "Manual journal not found.")
        if row["status"] != "pending_approval":
            raise LedgerRefused(409, f"This journal is already {row['status'].replace('_', ' ')}.")
        if row["created_by_user_id"] == actor.user_id:
            raise LedgerRefused(403, "Another Finance user must approve or reject a journal you made (maker/checker).")
        if not approve and not (reason or "").strip():
            raise LedgerRefused(422, "Say why it's rejected.")
        connection.execute(text("UPDATE journals SET status = :s, approved_by_user_id = :u, approved_at = clock_timestamp(), rejected_reason = :r WHERE id = :id"),
                           {"s": "posted" if approve else "rejected", "u": actor.user_id, "r": None if approve else reason.strip(), "id": journal_id})
        finance.audit(connection, actor, "manual_journal_approved" if approve else "manual_journal_rejected", "journal", journal_id,
                      before={"status": "pending_approval"}, after={"status": "posted" if approve else "rejected"}, reason=(reason or "").strip() or None)


# ---------------------------------------------------------------------------------------------------- reports
def _range(start: date | None, end: date | None) -> tuple[str, dict]:
    where, params = ["j.status = 'posted'"], {}
    if start:
        where.append("j.posted_on >= :start")
        params["start"] = start
    if end:
        where.append("j.posted_on <= :end")
        params["end"] = end
    return " AND ".join(where), params


def balances(start: date | None, end: date | None) -> list[dict]:
    """Per account: debits, credits and the balance on its normal side, for posted journals in the range."""
    clause, params = _range(start, end)
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text(
            "SELECT a.code, a.name, a.type, a.active, COALESCE(SUM(l.debit_minor), 0)::bigint AS debit, COALESCE(SUM(l.credit_minor), 0)::bigint AS credit"
            f" FROM ledger_accounts a LEFT JOIN journal_lines l ON l.account_code = a.code LEFT JOIN journals j ON j.id = l.journal_id AND {clause}"
            " WHERE l.id IS NULL OR j.id IS NOT NULL GROUP BY a.code ORDER BY a.code"), params).mappings().all()
    out = []
    for r in rows:
        balance = r["debit"] - r["credit"] if r["type"] in DEBIT_NORMAL else r["credit"] - r["debit"]
        out.append({**dict(r), "balance": balance})
    return out


def account_entries(code: str, start: date | None, end: date | None, limit: int = 1000) -> tuple[dict, int, list[dict]]:
    """An account's opening balance (before start) and its posted entries in the range with a running balance."""
    with agencies.application_engine().begin() as connection:
        account = connection.execute(text("SELECT code, name, type FROM ledger_accounts WHERE code = :c"), {"c": code}).mappings().one_or_none()
        if account is None:
            raise LedgerRefused(404, "Account not found.")
        sign = 1 if account["type"] in DEBIT_NORMAL else -1
        opening = 0
        if start:
            opening = sign * connection.scalar(text(
                "SELECT COALESCE(SUM(l.debit_minor - l.credit_minor), 0)::bigint FROM journal_lines l JOIN journals j ON j.id = l.journal_id"
                " WHERE j.status = 'posted' AND l.account_code = :c AND j.posted_on < :s"), {"c": code, "s": start})
        clause, params = _range(start, end)
        rows = connection.execute(text(
            "SELECT j.number, j.posted_on, j.description, j.source_type, b.reference AS booking_reference, l.debit_minor, l.credit_minor, l.memo"
            f" FROM journal_lines l JOIN journals j ON j.id = l.journal_id LEFT JOIN bookings b ON b.id = j.booking_id WHERE l.account_code = :c AND {clause}"
            " ORDER BY j.posted_on, j.number LIMIT :limit"), {**params, "c": code, "limit": limit}).mappings().all()
    running, entries = opening, []
    for r in rows:
        running += sign * (r["debit_minor"] - r["credit_minor"])
        entries.append({**dict(r), "balance": running})
    return dict(account), opening, entries


def journals(start: date | None, end: date | None, status: str | None = None, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    where, params = ["true"], {"limit": limit, "offset": offset}
    if start:
        where.append("j.posted_on >= :start")
        params["start"] = start
    if end:
        where.append("j.posted_on <= :end")
        params["end"] = end
    if status:
        where.append("j.status = :st")
        params["st"] = status
    clause = " AND ".join(where)
    with agencies.application_engine().begin() as connection:
        total = connection.scalar(text(f"SELECT count(*) FROM journals j WHERE {clause}"), params)
        heads = connection.execute(text(
            "SELECT j.*, b.reference AS booking_reference FROM journals j LEFT JOIN bookings b ON b.id = j.booking_id"
            f" WHERE {clause} ORDER BY j.posted_on DESC, j.number DESC LIMIT :limit OFFSET :offset"), params).mappings().all()
        ids = [h["id"] for h in heads]
        lines: dict[str, list[dict]] = {}
        for l in connection.execute(text("SELECT l.journal_id, l.account_code, a.name, l.debit_minor, l.credit_minor, l.memo FROM journal_lines l"
                                         " JOIN ledger_accounts a ON a.code = l.account_code WHERE l.journal_id = ANY(:ids) ORDER BY l.id"),
                                    {"ids": ids}).mappings().all():
            lines.setdefault(l["journal_id"], []).append(dict(l))
    return [{**dict(h), "lines": lines.get(h["id"], [])} for h in heads], total


def gaps(limit: int = 500) -> list[dict]:
    with agencies.application_engine().begin() as connection:
        return [dict(r) for r in connection.execute(text(
            "SELECT g.*, b.reference AS booking_reference FROM ledger_gaps g LEFT JOIN bookings b ON b.id = g.booking_id ORDER BY g.created_at DESC LIMIT :l"),
            {"l": limit}).mappings().all()]


def credit_and_debit_totals(rows: Iterable[dict]) -> tuple[int, int]:
    rows = list(rows)
    return sum(r["debit"] for r in rows), sum(r["credit"] for r in rows)
