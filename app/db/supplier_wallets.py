"""Supplier wallets (migration 0030, travelian#68). Integer paisa. Nothing here moves money or calls a supplier.

- Southair: the balance is what Southair's API last said (supplier_balance_snapshots), never calculated.
- SooperFare (no balance API): calculated = top-ups + adjustments - actual ticket charges (supplier_charges, 0028)
  recorded since tracking started (when the first top-up or adjustment was recorded). Shown as calculated, never as "verified".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies, finance

#: provider id -> (display name, how the balance is known)
SUPPLIERS: dict[str, tuple[str, str]] = {"southair": ("Southair", "api"), "sooperfare": ("SooperFare", "calculated")}


def threshold_key(provider: str) -> str:
    return f"low_balance_{provider}_minor"


@dataclass(frozen=True)
class Snapshot:
    balance_minor: int
    currency: str
    checked_at: datetime
    requested_by_user_id: str | None


@dataclass(frozen=True)
class Wallet:
    provider_id: str
    name: str
    mode: str  # api | calculated
    balance_minor: int | None
    currency: str
    as_of: datetime | None
    tracking_since: datetime | None
    threshold_minor: int | None

    @property
    def low(self) -> bool | None:
        if self.balance_minor is None or self.threshold_minor is None:
            return None
        return self.balance_minor < self.threshold_minor


def record_snapshot(provider: str, balance_minor: int, currency: str, actor: finance.Actor) -> Snapshot:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "INSERT INTO supplier_balance_snapshots (provider_id, balance_minor, currency, requested_by_user_id) VALUES (:p, :b, :c, :u)"
            " RETURNING balance_minor, currency, checked_at, requested_by_user_id"),
            {"p": provider, "b": balance_minor, "c": currency, "u": actor.user_id}).one()
        finance.audit(connection, actor, "supplier_balance_checked", "supplier_wallet", provider, after={"balance_minor": balance_minor, "currency": currency})
    return Snapshot(*row)


def _calculated(connection, provider: str) -> tuple[int | None, datetime | None, datetime | None]:
    """(balance, tracking since, last movement) from entries and charges; None balance when nothing is recorded yet."""
    since = connection.scalar(text("SELECT min(recorded_at) FROM supplier_wallet_entries WHERE provider_id = :p"), {"p": provider})
    if since is None:
        return None, None, None
    entries = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0)::bigint FROM supplier_wallet_entries WHERE provider_id = :p"),
                                {"p": provider})
    charges = connection.scalar(text("SELECT COALESCE(SUM(amount_minor), 0)::bigint FROM supplier_charges WHERE provider_id = :p AND recorded_at >= :s"),
                                {"p": provider, "s": since})
    last = connection.scalar(text(
        "SELECT GREATEST((SELECT max(recorded_at) FROM supplier_wallet_entries WHERE provider_id = :p),"
        " (SELECT max(recorded_at) FROM supplier_charges WHERE provider_id = :p AND recorded_at >= :s))"), {"p": provider, "s": since})
    return entries - charges, since, last


def wallet(provider: str, connection=None) -> Wallet:
    name, mode = SUPPLIERS[provider]

    def read(conn) -> Wallet:
        threshold = finance.get_settings(conn).get(threshold_key(provider))
        if mode == "api":
            row = conn.execute(text("SELECT balance_minor, currency, checked_at FROM supplier_balance_snapshots WHERE provider_id = :p"
                                    " ORDER BY checked_at DESC, id DESC LIMIT 1"), {"p": provider}).one_or_none()
            return Wallet(provider, name, mode, row[0] if row else None, row[1] if row else "PKR", row[2] if row else None, None, threshold)
        balance, since, last = _calculated(conn, provider)
        return Wallet(provider, name, mode, balance, "PKR", last, since, threshold)
    if connection is not None:
        return read(connection)
    with agencies.application_engine().begin() as conn:
        return read(conn)


def wallets() -> list[Wallet]:
    with agencies.application_engine().begin() as connection:
        return [wallet(provider, connection) for provider in SUPPLIERS]


def add_entry(provider: str, actor: finance.Actor, *, kind: str, amount_minor: int, reference: str | None, reason: str | None,
              occurred_at: datetime) -> Wallet:
    if SUPPLIERS.get(provider, ("", ""))[1] != "calculated":
        raise finance.FinanceRefused(422, "This supplier's balance comes from its own system; record top-ups there.", "not_calculated")
    if kind == "top_up" and amount_minor <= 0:
        raise finance.FinanceRefused(422, "Enter the amount paid in.", "invalid_amount")
    if kind == "adjustment" and (amount_minor == 0 or not (reason or "").strip()):
        raise finance.FinanceRefused(422, "Enter the correction (+ or -) and why.", "reason_required")
    if kind == "top_up" and not (reference or "").strip():
        raise finance.FinanceRefused(422, "Enter the bank or transfer reference of the top-up.", "reference_required")
    with agencies.application_engine().begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:p))"), {"p": f"wallet:{provider}"})
        before = wallet(provider, connection)
        entry_id = str(uuid4())
        connection.execute(text(
            "INSERT INTO supplier_wallet_entries (id, provider_id, kind, amount_minor, currency, reference, reason, occurred_at, recorded_by_user_id)"
            " VALUES (:id, :p, :k, :a, 'PKR', :ref, :reason, :at, :u)"),
            {"id": entry_id, "p": provider, "k": kind, "a": amount_minor, "ref": (reference or "").strip() or None,
             "reason": (reason or "").strip() or None, "at": occurred_at, "u": actor.user_id})
        after = wallet(provider, connection)
        finance.audit(connection, actor, f"supplier_{kind}_recorded", "supplier_wallet", provider,
                      before={"balance_minor": before.balance_minor}, after={"amount_minor": amount_minor, "balance_minor": after.balance_minor,
                                                                             "reference": (reference or "").strip() or None},
                      reason=(reason or "").strip() or None)
        return after


@dataclass(frozen=True)
class HistoryItem:
    at: datetime
    kind: str  # balance_check | top_up | adjustment | ticket
    amount_minor: int | None
    balance_minor: int | None
    reference: str | None
    reason: str | None
    booking_reference: str | None
    by_user_id: str | None


def history(provider: str, limit: int = 200) -> list[HistoryItem]:
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text("""
            SELECT * FROM (
              SELECT checked_at AS at, 'balance_check' AS kind, NULL::bigint AS amount, balance_minor AS balance, NULL AS reference,
                     NULL AS reason, NULL AS booking_reference, requested_by_user_id AS by_user
              FROM supplier_balance_snapshots WHERE provider_id = :p
              UNION ALL
              SELECT occurred_at, kind, amount_minor, NULL, reference, reason, NULL, recorded_by_user_id
              FROM supplier_wallet_entries WHERE provider_id = :p
              UNION ALL
              SELECT s.recorded_at, 'ticket', -s.amount_minor, NULL, s.supplier_reference, s.reason, b.reference, s.recorded_by_user_id
              FROM supplier_charges s JOIN bookings b ON b.id = s.booking_id WHERE s.provider_id = :p
            ) h ORDER BY at DESC LIMIT :limit"""), {"p": provider, "limit": limit}).all()
    return [HistoryItem(*row) for row in rows]


def alert_state(provider: str, low: bool, now: datetime) -> bool:
    """Record whether the balance is low; True only on the change from not-low to low (alert once per drop)."""
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("SELECT low_since FROM supplier_wallet_alerts WHERE provider_id = :p FOR UPDATE"), {"p": provider}).one_or_none()
        was_low = bool(row and row[0])
        if low and not was_low:
            connection.execute(text("INSERT INTO supplier_wallet_alerts (provider_id, low_since, alerted_at) VALUES (:p, :n, :n)"
                                    " ON CONFLICT (provider_id) DO UPDATE SET low_since = :n, alerted_at = :n"), {"p": provider, "n": now})
            return True
        if not low and was_low:
            connection.execute(text("UPDATE supplier_wallet_alerts SET low_since = NULL WHERE provider_id = :p"), {"p": provider})
        return False
