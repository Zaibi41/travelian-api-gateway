"""Supplier balances and the per-supplier ledger (migration 0037). Integer paisa. Nothing here moves money or calls a supplier.

A supplier's balance moves only three ways (never edited by hand):
  1. a top-up (supplier_wallet_entries, kind top_up; an adjustment corrects with a reason);
  2. a Ticket Sale Entry deduction (ticket_sale_entries: confirmed, issued, with this supplier);
  3. a real booking's charge (supplier_charges: recorded at issue, automatic or by staff, or by a Supplier payment
     settlement). A refund credit applied to a settlement (supplier_refund_applications) pays part of that charge, so it
     shows on the ledger as money back in.

balance_mode "manual" (SooperFare, direct airline IDs): calculated from the opening balance and the movements since
tracking started (the opening balance being set, or the first top-up). Before that the balance is not known (None).
balance_mode "api" (Southair): the balance its API last returned (supplier_balance_snapshots); the movements above are
shown for the record but don't change it (Southair's own balance already includes them).

Negative-balance block (D52): every deduction checks `covers()` first, in the same transaction, under a per-supplier
lock. No override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.db import agencies, finance

STATUSES = ("ok", "low", "negative", "not_set_up")


@dataclass(frozen=True)
class Supplier:
    id: str
    name: str
    section: str  # api (flights by API) | direct (own airline IDs) | b2b (B2B / group portals) | hotels
    booking_mode: str  # api | manual
    balance_mode: str  # api | manual
    settlement_tag: str | None
    opening_balance_minor: int
    opening_set_at: datetime | None
    low_threshold_minor: int | None
    active: bool


@dataclass
class Balance:
    supplier: Supplier
    current_minor: int | None
    opening_minor: int | None
    topped_up_minor: int
    deducted_minor: int
    tracking_since: datetime | None
    last_updated: datetime | None

    @property
    def status(self) -> str:
        if self.current_minor is None:
            return "not_set_up"
        if self.current_minor < 0:
            return "negative"
        if self.supplier.low_threshold_minor is not None and self.current_minor < self.supplier.low_threshold_minor:
            return "low"
        return "ok"


def _supplier(row) -> Supplier:
    return Supplier(row["id"], row["name"], row["section"], row["booking_mode"], row["balance_mode"], row["settlement_tag"],
                    row["opening_balance_minor"], row["opening_set_at"], row["low_threshold_minor"], row["active"])


def suppliers(connection=None, *, include_inactive: bool = False) -> list[Supplier]:
    sql = text("SELECT * FROM suppliers" + ("" if include_inactive else " WHERE active") +
               " ORDER BY CASE section WHEN 'api' THEN 0 WHEN 'direct' THEN 1 WHEN 'b2b' THEN 2 ELSE 3 END, created_at, name")
    if connection is not None:
        return [_supplier(r) for r in connection.execute(sql).mappings().all()]
    with agencies.application_engine().begin() as conn:
        return [_supplier(r) for r in conn.execute(sql).mappings().all()]


def supplier(connection, supplier_id: str) -> Supplier | None:
    row = connection.execute(text("SELECT * FROM suppliers WHERE id = :s"), {"s": supplier_id}).mappings().one_or_none()
    return _supplier(row) if row else None


def _tracking_since(connection, s: Supplier) -> datetime | None:
    """The opening balance's date when set (it is the balance then; earlier movements are history only), else the first
    top-up or adjustment (the balance counts from zero then, as SooperFare's did before migration 0037)."""
    if s.opening_set_at is not None:
        return s.opening_set_at
    return connection.scalar(text("SELECT min(recorded_at) FROM supplier_wallet_entries WHERE provider_id = :p"), {"p": s.id})


def _movements(connection, s: Supplier, since: datetime | None) -> tuple[int, int, datetime | None]:
    """(topped up, deducted, last movement) since `since` (all time when None)."""
    p = {"p": s.id, "s": since}
    after = "" if since is None else " AND {col} >= :s"
    entries = connection.execute(text(
        "SELECT COALESCE(SUM(amount_minor) FILTER (WHERE amount_minor > 0), 0)::bigint, COALESCE(-SUM(amount_minor) FILTER (WHERE amount_minor < 0), 0)::bigint,"
        " max(recorded_at) FROM supplier_wallet_entries WHERE provider_id = :p" + after.format(col="recorded_at")), p).one()
    credits = connection.execute(text(
        "SELECT COALESCE(SUM(a.amount_minor), 0)::bigint, max(a.applied_at) FROM supplier_refund_applications a"
        " JOIN supplier_refunds r ON r.id = a.refund_id WHERE r.supplier_id = :p" + after.format(col="a.applied_at")), p).one()
    charges = connection.execute(text(
        "SELECT COALESCE(SUM(amount_minor), 0)::bigint, max(recorded_at) FROM supplier_charges WHERE provider_id = :p"
        + after.format(col="recorded_at")), p).one()
    sales = connection.execute(text(
        "SELECT COALESCE(SUM(cost_minor), 0)::bigint, max(confirmed_at) FROM ticket_sale_entries"
        " WHERE supplier_id = :p AND state = 'confirmed' AND ticket_status = 'issued'" + after.format(col="confirmed_at")), p).one()
    topped = entries[0] + credits[0]
    deducted = entries[1] + charges[0] + sales[0]
    last = max((t for t in (entries[2], credits[1], charges[1], sales[1]) if t is not None), default=None)
    return topped, deducted, last


def balance(connection, s: Supplier) -> Balance:
    if s.balance_mode == "api":
        snap = connection.execute(text("SELECT balance_minor, checked_at FROM supplier_balance_snapshots WHERE provider_id = :p"
                                       " ORDER BY checked_at DESC, id DESC LIMIT 1"), {"p": s.id}).one_or_none()
        topped, deducted, last = _movements(connection, s, None)
        return Balance(s, snap[0] if snap else None, None, topped, deducted, None, snap[1] if snap else last)
    since = _tracking_since(connection, s)
    if since is None:
        return Balance(s, None, None, 0, 0, None, None)
    topped, deducted, last = _movements(connection, s, since)
    opening = s.opening_balance_minor if s.opening_set_at is not None else 0
    return Balance(s, opening + topped - deducted, opening, topped, deducted, since, max(t for t in (last, s.opening_set_at, since) if t))


def balances(*, include_inactive: bool = False) -> list[Balance]:
    with agencies.application_engine().begin() as connection:
        return [balance(connection, s) for s in suppliers(connection, include_inactive=include_inactive)]


def lock(connection, supplier_id: str) -> None:
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"wallet:{supplier_id}"})


SECTIONS = ("api", "direct", "b2b", "hotels")
NEGATIVE = "negative_balance"


def covers(connection, supplier_id: str, amount_minor: int, *, credit_minor: int = 0, what: str = "This") -> Balance | None:
    """D52: refuse (409, negative_balance) when deducting `amount_minor` (less `credit_minor` paid by a refund credit) would
    take the supplier's balance below zero. Call under lock(). No override.
    Not refused: a manual supplier whose balance isn't set up yet (no opening balance or top-up recorded: the balance
    isn't known, so it can't be shown to go negative). An API supplier (Southair) with no balance read yet is refused:
    refresh its balance first."""
    s = supplier(connection, supplier_id)
    if s is None:
        raise finance.FinanceRefused(422, "Choose a supplier from the list.", "unknown_supplier")
    b = balance(connection, s)
    if b.current_minor is None:
        if s.balance_mode == "api":
            raise finance.FinanceRefused(409, f"{s.name}'s balance hasn't been read yet. Refresh it on Supplier Balances, then try again. "
                                              "Nothing was saved.", NEGATIVE)
        return b
    after = b.current_minor + credit_minor - amount_minor
    if after < 0:
        rupees = lambda m: f"Rs {m / 100:,.0f}" if m % 100 == 0 else f"Rs {m / 100:,.2f}"  # noqa: E731
        raise finance.FinanceRefused(409, f"{what} would take {s.name}'s balance to {rupees(after)} (now {rupees(b.current_minor)}). "
                                          f"Top up {s.name} first. Nothing was saved.", NEGATIVE)
    return b


# ------------------------------------------------------------------------------------------------------- manage suppliers
_ID = re.compile(r"[^a-z0-9]+")


def add_supplier(actor: finance.Actor, *, name: str, section: str, settlement_tag: str | None, low_threshold_minor: int | None) -> Supplier:
    name = " ".join((name or "").split())
    if not 2 <= len(name) <= 120:
        raise finance.FinanceRefused(422, "Enter the supplier's name.", "name_required")
    if section not in SECTIONS:
        raise finance.FinanceRefused(422, "Choose the supplier type.", "invalid_section")
    supplier_id = _ID.sub("", name.lower())[:40] or "supplier"
    with agencies.application_engine().begin() as connection:
        if connection.scalar(text("SELECT 1 FROM suppliers WHERE lower(name) = lower(:n)"), {"n": name}):
            raise finance.FinanceRefused(409, f"{name} is already on the list.", "duplicate_supplier")
        base, n = supplier_id, 2
        while connection.scalar(text("SELECT 1 FROM suppliers WHERE id = :s"), {"s": supplier_id}):
            supplier_id, n = f"{base[:37]}{n}", n + 1
        # A new supplier has no booking API in this system: its booking and balance are manual.
        connection.execute(text(
            "INSERT INTO suppliers (id, name, section, booking_mode, balance_mode, settlement_tag, low_threshold_minor, created_by_user_id)"
            " VALUES (:id, :n, :sec, 'manual', 'manual', :tag, :th, :u)"),
            {"id": supplier_id, "n": name, "sec": section, "tag": settlement_tag, "th": low_threshold_minor, "u": actor.user_id})
        finance.audit(connection, actor, "supplier_added", "supplier", supplier_id,
                      after={"name": name, "section": section, "settlement_tag": settlement_tag, "low_threshold_minor": low_threshold_minor})
        return supplier(connection, supplier_id)  # type: ignore[return-value]


def update_supplier(actor: finance.Actor, supplier_id: str, changes: dict[str, Any]) -> Supplier:
    """Low-balance threshold and the DTS/IATA reference tag. The opening balance is set once (set_opening)."""
    allowed = {k: v for k, v in changes.items() if k in ("low_threshold_minor", "settlement_tag")}
    with agencies.application_engine().begin() as connection:
        before = supplier(connection, supplier_id)
        if before is None:
            raise finance.FinanceRefused(404, "Supplier not found.", "unknown_supplier")
        for key, value in allowed.items():
            connection.execute(text(f"UPDATE suppliers SET {key} = :v WHERE id = :s"), {"v": value, "s": supplier_id})
        if "low_threshold_minor" in allowed and supplier_id in ("southair", "sooperfare"):
            # Finance > Wallets (travelian#68) reads the same level from the Finance settings.
            import json

            connection.execute(text("INSERT INTO finance_settings (key, value, updated_by_user_id, updated_at) VALUES (:k, CAST(:v AS jsonb), :u, now())"
                                    " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by_user_id = EXCLUDED.updated_by_user_id,"
                                    " updated_at = now()"),
                               {"k": f"low_balance_{supplier_id}_minor", "v": json.dumps(allowed["low_threshold_minor"]), "u": actor.user_id})
        finance.audit(connection, actor, "supplier_updated", "supplier", supplier_id,
                      before={k: getattr(before, k) for k in allowed}, after=allowed)
        return supplier(connection, supplier_id)  # type: ignore[return-value]


def set_opening(actor: finance.Actor, supplier_id: str, amount_minor: int, as_of: datetime) -> Supplier:
    """Start tracking a manual supplier's balance from the amount it held on `as_of`. Once only: later differences are
    adjustments (with a reason), so the history never changes."""
    with agencies.application_engine().begin() as connection:
        lock(connection, supplier_id)
        s = supplier(connection, supplier_id)
        if s is None:
            raise finance.FinanceRefused(404, "Supplier not found.", "unknown_supplier")
        if s.balance_mode != "manual":
            raise finance.FinanceRefused(422, f"{s.name}'s balance comes from its own system.", "not_manual")
        if s.opening_set_at is not None:
            raise finance.FinanceRefused(409, "The opening balance is already set. Record an adjustment (with a reason) to correct it.",
                                         "opening_already_set")
        connection.execute(text("UPDATE suppliers SET opening_balance_minor = :a, opening_set_at = :t WHERE id = :s"),
                           {"a": amount_minor, "t": as_of, "s": supplier_id})
        finance.audit(connection, actor, "supplier_opening_set", "supplier", supplier_id, after={"amount_minor": amount_minor, "as_of": as_of})
        return supplier(connection, supplier_id)  # type: ignore[return-value]


def add_top_up(actor: finance.Actor, supplier_id: str, *, kind: str, amount_minor: int, reference: str | None, reason: str | None,
               occurred_at: datetime, paid_from_account_id: str | None) -> Balance:
    """A top-up (adds) or an adjustment (+/-, with a reason). Adjustments that take the balance below zero are refused (D52)."""
    if kind == "top_up" and amount_minor <= 0:
        raise finance.FinanceRefused(422, "Enter the amount paid in.", "invalid_amount")
    if kind == "top_up" and not (reference or "").strip():
        raise finance.FinanceRefused(422, "Enter the bank or transfer reference of the top-up.", "reference_required")
    if kind == "adjustment" and (amount_minor == 0 or not (reason or "").strip()):
        raise finance.FinanceRefused(422, "Enter the correction (+ or -) and why.", "reason_required")
    from uuid import uuid4

    with agencies.application_engine().begin() as connection:
        lock(connection, supplier_id)
        s = supplier(connection, supplier_id)
        if s is None:
            raise finance.FinanceRefused(404, "Supplier not found.", "unknown_supplier")
        if s.balance_mode != "manual":
            raise finance.FinanceRefused(422, f"{s.name}'s balance comes from its own system; record top-ups there.", "not_manual")
        if paid_from_account_id and not connection.scalar(text("SELECT 1 FROM payment_accounts WHERE id = :a"), {"a": paid_from_account_id}):
            raise finance.FinanceRefused(422, "Choose the bank account the top-up was paid from.", "unknown_account")
        if amount_minor < 0:
            covers(connection, supplier_id, -amount_minor, what="This adjustment")
        before = balance(connection, s)
        entry_id = str(uuid4())
        connection.execute(text(
            "INSERT INTO supplier_wallet_entries (id, provider_id, kind, amount_minor, currency, reference, reason, occurred_at, recorded_by_user_id,"
            " paid_from_account_id) VALUES (:id, :p, :k, :a, 'PKR', :ref, :reason, :at, :u, :acc)"),
            {"id": entry_id, "p": supplier_id, "k": kind, "a": amount_minor, "ref": (reference or "").strip() or None,
             "reason": (reason or "").strip() or None, "at": occurred_at, "u": actor.user_id, "acc": paid_from_account_id})
        after = balance(connection, s)
        finance.audit(connection, actor, f"supplier_{kind}_recorded", "supplier_wallet", supplier_id,
                      before={"balance_minor": before.current_minor},
                      after={"amount_minor": amount_minor, "balance_minor": after.current_minor, "reference": (reference or "").strip() or None,
                             "paid_from_account_id": paid_from_account_id}, reason=(reason or "").strip() or None)
        return after


# ------------------------------------------------------------------------------------------------------------- ledger
@dataclass
class LedgerItem:
    at: datetime
    kind: str  # opening | top_up | adjustment | refund_credit | booking_charge | ticket_sale | ticket_sale_reversal | balance_check
    amount_minor: int | None  # + adds, - deducts; None for a balance check
    balance_minor: int | None  # running balance after this item (manual suppliers, from tracking start); API: the read balance
    description: str
    reference: str | None = None
    booking_reference: str | None = None
    ticket_sale_number: int | None = None
    by_user_id: str | None = None
    by_role: str | None = None
    counted: bool = True  # False: before tracking started (shown, not in the balance)
    details: dict[str, Any] = field(default_factory=dict)


def ledger(supplier_id: str, limit: int = 500) -> tuple[Supplier, Balance, list[LedgerItem]]:
    """Every balance-affecting event for one supplier, oldest first with a running balance (newest first for display is the
    caller's choice). Each deduction says what caused it: a Ticket Sale Entry, a booking issued (automatic or by staff), or
    a Supplier payment settlement."""
    with agencies.application_engine().begin() as connection:
        s = supplier(connection, supplier_id)
        if s is None:
            raise finance.FinanceRefused(404, "Supplier not found.", "unknown_supplier")
        b = balance(connection, s)
        rows = connection.execute(text("""
            SELECT * FROM (
              SELECT e.recorded_at AS at, e.kind AS kind, e.amount_minor AS amount, e.reference AS ref, e.reason AS reason,
                     NULL::text AS booking_ref, NULL::bigint AS tse, e.recorded_by_user_id AS by_user, NULL::text AS source,
                     a.label AS account, NULL::text AS refund_no, NULL::timestamptz AS origin_at
              FROM supplier_wallet_entries e LEFT JOIN payment_accounts a ON a.id = e.paid_from_account_id WHERE e.provider_id = :p
              UNION ALL
              SELECT ap.applied_at, 'refund_credit', ap.amount_minor, NULL, r.reason, b.reference, NULL, ap.applied_by_user_id, NULL, NULL,
                     r.number::text, NULL
              FROM supplier_refund_applications ap JOIN supplier_refunds r ON r.id = ap.refund_id JOIN bookings b ON b.id = ap.booking_id
              WHERE r.supplier_id = :p
              UNION ALL
              SELECT c.recorded_at, 'booking_charge', -c.amount_minor, c.supplier_reference, c.reason, b.reference, NULL, c.recorded_by_user_id,
                     c.source, NULL, NULL, NULL
              FROM supplier_charges c JOIN bookings b ON b.id = c.booking_id WHERE c.provider_id = :p
              UNION ALL
              SELECT t.confirmed_at, 'ticket_sale', -t.cost_minor, t.pnr, t.route, NULL, t.number, t.confirmed_by_user_id, NULL, NULL, NULL, NULL
              FROM ticket_sale_entries t WHERE t.supplier_id = :p AND t.state IN ('confirmed', 'reversed', 'deleted') AND t.confirmed_at IS NOT NULL
                AND t.ticket_status = 'issued'
              UNION ALL
              SELECT t.reversed_at, 'ticket_sale_reversal', t.cost_minor, t.pnr, t.correction_reason, NULL, t.number, t.reversed_by_user_id, t.state,
                     NULL, NULL, t.confirmed_at
              FROM ticket_sale_entries t WHERE t.supplier_id = :p AND t.state IN ('reversed', 'deleted') AND t.confirmed_at IS NOT NULL
                AND t.ticket_status = 'issued'
              UNION ALL
              SELECT checked_at, 'balance_check', NULL, NULL, NULL, NULL, NULL, requested_by_user_id, NULL, NULL, balance_minor::text, NULL
              FROM supplier_balance_snapshots WHERE provider_id = :p
            ) h ORDER BY at, CASE kind WHEN 'top_up' THEN 0 WHEN 'adjustment' THEN 0 WHEN 'refund_credit' THEN 0
                                     WHEN 'ticket_sale_reversal' THEN 0 ELSE 1 END, kind LIMIT :limit"""), {"p": supplier_id, "limit": limit}).mappings().all()
        roles = _roles(connection, {r["by_user"] for r in rows if r["by_user"]})
    items: list[LedgerItem] = []
    running = b.opening_minor if s.balance_mode == "manual" and b.tracking_since is not None else None
    if s.balance_mode == "manual" and s.opening_set_at is not None:
        items.append(LedgerItem(s.opening_set_at, "opening", s.opening_balance_minor, running, "Opening balance"))
    for r in rows:
        kind, amount = r["kind"], r["amount"]
        # A reversal counts only if the deduction it undoes counted (so the running balance matches the balance).
        counted = b.tracking_since is None or (r["origin_at"] or r["at"]) >= b.tracking_since
        description, details = _describe(r)
        if kind == "balance_check":
            items.append(LedgerItem(r["at"], kind, None, int(r["refund_no"]), "Balance read from the supplier's system", by_user_id=r["by_user"],
                                    by_role=roles.get(r["by_user"]) if r["by_user"] else "system"))
            continue
        if s.balance_mode == "manual" and counted and running is not None:
            running += amount
        items.append(LedgerItem(r["at"], kind, amount, running if s.balance_mode == "manual" and counted else None, description,
                                reference=r["ref"], booking_reference=r["booking_ref"], ticket_sale_number=r["tse"], by_user_id=r["by_user"],
                                by_role=roles.get(r["by_user"]) if r["by_user"] else "system", counted=counted, details=details))
    items.sort(key=lambda i: i.at)
    return s, b, items


def _describe(r) -> tuple[str, dict[str, Any]]:
    kind = r["kind"]
    if kind == "top_up":
        return ("Top-up" + (f" from {r['account']}" if r["account"] else ""), {})
    if kind == "adjustment":
        return (f"Adjustment: {r['reason']}" if r["reason"] else "Adjustment", {})
    if kind == "refund_credit":
        return (f"Refund credit #{r['refund_no']} applied to booking {r['booking_ref']}", {"refund_number": r["refund_no"]})
    if kind == "ticket_sale_reversal":
        how = "deleted" if r["source"] == "deleted" else "reversed (corrected)"
        return (f"Ticket Sale Entry #{r['tse']} {how}" + (f": {r['reason']}" if r["reason"] else ""), {})
    if kind == "ticket_sale":
        return (f"Ticket Sale Entry #{r['tse']}" + (f" ({r['reason']})" if r["reason"] else ""), {})
    if kind == "booking_charge":
        source, by = r["source"], r["by_user"]
        if r["reason"] and r["reason"].startswith(SETTLEMENT_REASON):
            how = "Supplier payment settlement"
        elif source == "issuance":
            how = "issued automatically" if by is None else "issued by staff"
        elif source == "supplier_report":
            how = "charge from the supplier's report"
        else:
            how = "charge recorded by Finance"
        return (f"Booking {r['booking_ref']}: {how}", {"source": source})
    return (kind, {})


SETTLEMENT_REASON = "Supplier payment settlement"


def _roles(connection, user_ids: set[str]) -> dict[str, str]:
    if not user_ids:
        return {}
    rows = connection.execute(text("SELECT u.id, r.name FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = ANY(:ids)"),
                              {"ids": list(user_ids)}).all()
    return {row[0]: row[1] for row in rows}
