"""Reconciliation (migration 0031, travelian#71). Integer paisa. Reconciliation never changes a payment or a booking;
the only money record it adds is a PayFast fee taken from PayFast's own settlement line (payment_fees, source
'settlement'), so profit uses the real fee.

1. PayFast: each settlement line is matched to a card payment by transaction id (or basket id). Gross amount must equal
   what the customer was charged (fare + card fee); a different amount, an unknown line or a card payment missing from
   the file goes on the mismatch list.
2. Bank: each credit on the statement of a receiving account gets a SUGGESTED payment (same account, same amount,
   received within 5 days; the booking or payer reference in the line text decides between several). Staff confirm or
   reject each suggestion. Credits with no match, and payments into that account with no credit, are mismatches.
3. Suppliers: issued tickets with no recorded supplier charge, charges that differ from the stored cost, and (from a
   supplier statement) lines that differ from what Travelian recorded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.db import agencies, finance
from app.services.statement_csv import Line

BANK_DAYS = 5
KINDS = ("payfast", "bank", "supplier")


class ReconRefused(Exception):
    def __init__(self, status: int, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.status, self.message, self.problems = status, message, problems or []


def _mismatch(connection, kind: str, key: str, detail: str, *, line_id=None, payment_id=None, booking_id=None, expected=None, actual=None) -> bool:
    """Add a mismatch once (dedupe_key). Returns True when it is new."""
    row = connection.execute(text(
        "INSERT INTO recon_mismatches (id, kind, dedupe_key, line_id, payment_id, booking_id, expected_minor, actual_minor, detail)"
        " VALUES (:id, :k, :key, :l, :p, :b, :e, :a, :d) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id"),
        {"id": str(uuid4()), "k": kind, "key": f"{kind}:{key}"[:200], "l": line_id, "p": payment_id, "b": booking_id, "e": expected, "a": actual,
         "d": detail}).first()
    return row is not None


def _money(minor: int | None) -> str:
    if minor is None:
        return "—"
    sign, value = ("-", -minor) if minor < 0 else ("", minor)
    return f"{sign}Rs {value // 100:,}" + (f".{value % 100:02d}" if value % 100 else "")


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    lines: int
    matched: int
    suggested: int
    unmatched: int
    mismatches_added: int
    fees_recorded: int


def _start_import(connection, kind: str, account_id: str | None, provider_id: str | None, file_name: str, content: bytes,
                  column_map: dict[str, Any], count: int, actor: finance.Actor) -> str:
    digest = hashlib.sha256(content).hexdigest()
    exists = connection.scalar(text(
        "SELECT 1 FROM recon_imports WHERE kind = :k AND COALESCE(account_id, '') = COALESCE(:a, '') AND COALESCE(provider_id, '') = COALESCE(:p, '')"
        " AND sha256 = :s"), {"k": kind, "a": account_id, "p": provider_id, "s": digest})
    if exists:
        raise ReconRefused(409, "This file was already imported.")
    import_id = str(uuid4())
    connection.execute(text(
        "INSERT INTO recon_imports (id, kind, account_id, provider_id, file_name, sha256, column_map, row_count, imported_by_user_id)"
        " VALUES (:id, :k, :a, :p, :f, :s, CAST(:m AS jsonb), :n, :u)"),
        {"id": import_id, "k": kind, "a": account_id, "p": provider_id, "f": file_name[:200], "s": digest, "m": json.dumps(column_map), "n": count,
         "u": actor.user_id})
    finance.audit(connection, actor, "statement_imported", "recon_import", import_id,
                  after={"kind": kind, "file_name": file_name[:200], "rows": count, "account_id": account_id, "provider_id": provider_id})
    return import_id


def _insert_line(connection, import_id: str, line: Line, status: str, payment_id=None, booking_id=None, actor: finance.Actor | None = None) -> str:
    line_id = str(uuid4())
    connection.execute(text(
        "INSERT INTO recon_lines (id, import_id, line_no, reference, description, occurred_on, amount_minor, fee_minor, net_minor, payout_on, status,"
        " payment_id, booking_id, matched_by_user_id, matched_at)"
        " VALUES (:id, :i, :n, :r, :d, :o, :a, :f, :net, :po, :s, :p, :b, :u, CASE WHEN :is_matched THEN clock_timestamp() END)"),
        {"id": line_id, "i": import_id, "n": line.line_no, "r": (line.reference or "")[:200] or None, "d": line.description, "o": line.occurred_on,
         "a": line.amount_minor, "f": line.fee_minor, "net": line.net_minor, "po": line.payout_on, "s": status, "p": payment_id, "b": booking_id,
         "u": actor.user_id if (actor and status == "matched") else None, "is_matched": status == "matched"})
    return line_id


# ---------------------------------------------------------------------------------------------------- 1. PayFast
def import_payfast(file_name: str, content: bytes, column_map: dict[str, Any], lines: list[Line], actor: finance.Actor) -> ImportResult:
    if not column_map.get("reference"):
        raise ReconRefused(422, "Choose the column with PayFast's transaction id.")
    matched = unmatched = added = fees = 0
    with agencies.application_engine().begin() as connection:
        import_id = _start_import(connection, "payfast", None, None, file_name, content, column_map, len(lines), actor)
        seen: set[str] = set()
        for line in lines:
            ref = (line.reference or "").strip()
            payment = connection.execute(text(
                "SELECT p.id, p.booking_id, p.amount_minor, p.card_fee_minor FROM booking_payments p WHERE p.method = 'payfast'"
                " AND (lower(p.reference) = lower(:r) OR p.booking_id IN (SELECT a.booking_id FROM payfast_attempts a WHERE a.basket_id = :r"
                " AND (a.transaction_id IS NULL OR lower(a.transaction_id) = lower(p.reference)))) ORDER BY p.received_at LIMIT 1"),
                {"r": ref}).mappings().one_or_none() if ref else None
            if payment is None:
                line_id = _insert_line(connection, import_id, line, "unmatched")
                unmatched += 1
                added += _mismatch(connection, "payfast_line_not_found", f"{ref or line_id}",
                                   f"PayFast line {ref or '(no id)'} for {_money(line.amount_minor)} matches no card payment in Travelian.",
                                   line_id=line_id, actual=line.amount_minor)
                continue
            seen.add(payment["id"])
            charged = payment["amount_minor"] + (payment["card_fee_minor"] or 0)
            line_id = _insert_line(connection, import_id, line, "matched", payment["id"], payment["booking_id"], actor)
            matched += 1
            if line.amount_minor != charged:
                added += _mismatch(connection, "payfast_amount_differs", payment["id"],
                                   f"PayFast says {_money(line.amount_minor)}; Travelian recorded {_money(charged)} charged.",
                                   line_id=line_id, payment_id=payment["id"], booking_id=payment["booking_id"], expected=charged, actual=line.amount_minor)
            if line.fee_minor is not None and line.net_minor is not None and line.amount_minor - line.fee_minor != line.net_minor:
                added += _mismatch(connection, "payfast_net_differs", line_id,
                                   f"Amount {_money(line.amount_minor)} − fee {_money(line.fee_minor)} isn't the net {_money(line.net_minor)}.",
                                   line_id=line_id, payment_id=payment["id"], expected=line.amount_minor - line.fee_minor, actual=line.net_minor)
            already = connection.scalar(text("SELECT 1 FROM payment_fees WHERE payment_id = :p AND source = 'settlement' LIMIT 1"), {"p": payment["id"]})
            if line.fee_minor and line.fee_minor > 0 and not already:
                finance.add_payment_fee(payment["id"], actor, amount_minor=line.fee_minor, source="settlement",
                                        reason=f"PayFast settlement {file_name[:120]}, line {line.line_no}", connection=connection)
                fees += 1
        dates = [line.occurred_on for line in lines if line.occurred_on]
        if dates:
            missing = connection.execute(text(
                "SELECT p.id, p.booking_id, p.amount_minor + p.card_fee_minor AS charged, p.reference FROM booking_payments p"
                " WHERE p.method = 'payfast' AND (p.received_at AT TIME ZONE 'Asia/Karachi')::date BETWEEN :a AND :b"
                " AND NOT EXISTS (SELECT 1 FROM recon_lines l WHERE l.payment_id = p.id)"), {"a": min(dates), "b": max(dates)}).mappings().all()
            for row in missing:
                if row["id"] in seen:
                    continue
                added += _mismatch(connection, "payment_not_in_payfast", row["id"],
                                   f"Card payment {row['reference']} ({_money(row['charged'])}) isn't in PayFast's file for these dates.",
                                   payment_id=row["id"], booking_id=row["booking_id"], expected=row["charged"])
    return ImportResult(import_id, len(lines), matched, 0, unmatched, added, fees)


# ---------------------------------------------------------------------------------------------------- 2. Bank
def import_bank(account_id: str, file_name: str, content: bytes, column_map: dict[str, Any], lines: list[Line], actor: finance.Actor) -> ImportResult:
    suggested = unmatched = added = 0
    with agencies.application_engine().begin() as connection:
        account = connection.execute(text("SELECT id, method, label FROM payment_accounts WHERE id = :a"), {"a": account_id}).mappings().one_or_none()
        if account is None or account["method"] not in ("bank_transfer", "jazzcash", "1link"):
            raise ReconRefused(422, "Choose the receiving account this statement belongs to.")
        import_id = _start_import(connection, "bank", account_id, None, file_name, content, column_map, len(lines), actor)
        taken: set[str] = set(connection.scalars(text("SELECT payment_id FROM recon_lines WHERE payment_id IS NOT NULL AND status IN ('matched', 'suggested')")))
        for line in lines:
            candidates = connection.execute(text(
                "SELECT p.id, p.booking_id, p.reference, b.reference AS booking_reference, (p.received_at AT TIME ZONE 'Asia/Karachi')::date AS day"
                " FROM booking_payments p JOIN bookings b ON b.id = p.booking_id WHERE p.account_id = :a AND p.amount_minor = :amt"
                + (" AND abs((p.received_at AT TIME ZONE 'Asia/Karachi')::date - CAST(:d AS date)) <= :days" if line.occurred_on else "")),
                {"a": account_id, "amt": line.amount_minor, "d": line.occurred_on, "days": BANK_DAYS}).mappings().all()
            candidates = [c for c in candidates if c["id"] not in taken]
            words = f"{line.reference or ''} {line.description or ''}".lower()
            strong = [c for c in candidates if c["booking_reference"].lower() in words or (c["reference"] and c["reference"].lower() in words)]
            pick = strong[0] if len(strong) == 1 else (candidates[0] if len(candidates) == 1 else None)
            if pick is not None:
                taken.add(pick["id"])
                _insert_line(connection, import_id, line, "suggested", pick["id"], pick["booking_id"])
                suggested += 1
            else:
                line_id = _insert_line(connection, import_id, line, "unmatched")
                unmatched += 1
                added += _mismatch(connection, "bank_credit_unmatched", line_id,
                                   f"{_money(line.amount_minor)} arrived in {account['label']} on {line.occurred_on or 'an unknown date'} "
                                   f"({(line.description or line.reference or 'no details')[:80]}) and matches no recorded payment"
                                   + (f" ({len(candidates)} possible)" if candidates else "") + ".",
                                   line_id=line_id, actual=line.amount_minor)
        dates = [line.occurred_on for line in lines if line.occurred_on]
        if dates:
            for row in connection.execute(text(
                    "SELECT p.id, p.booking_id, p.amount_minor, p.reference FROM booking_payments p WHERE p.account_id = :a"
                    " AND (p.received_at AT TIME ZONE 'Asia/Karachi')::date BETWEEN :s AND :e"
                    " AND NOT EXISTS (SELECT 1 FROM recon_lines l WHERE l.payment_id = p.id AND l.status IN ('matched', 'suggested'))"),
                    {"a": account_id, "s": min(dates), "e": max(dates)}).mappings().all():
                added += _mismatch(connection, "payment_not_in_bank", row["id"],
                                   f"Payment {row['reference']} ({_money(row['amount_minor'])}) recorded into {account['label']} isn't on the statement.",
                                   payment_id=row["id"], booking_id=row["booking_id"], expected=row["amount_minor"])
    return ImportResult(import_id, len(lines), 0, suggested, unmatched, added, 0)


def decide_suggestion(line_id: str, actor: finance.Actor, accept: bool) -> None:
    """Staff confirm (matched) or reject (unmatched + a mismatch) a suggested bank match. Audited."""
    with agencies.application_engine().begin() as connection:
        line = connection.execute(text("SELECT l.*, i.account_id FROM recon_lines l JOIN recon_imports i ON i.id = l.import_id WHERE l.id = :id FOR UPDATE OF l"),
                                  {"id": line_id}).mappings().one_or_none()
        if line is None:
            raise ReconRefused(404, "Statement line not found.")
        if line["status"] != "suggested":
            raise ReconRefused(409, "This line has no suggestion waiting.")
        if accept:
            connection.execute(text("UPDATE recon_lines SET status = 'matched', matched_by_user_id = :u, matched_at = clock_timestamp() WHERE id = :id"),
                               {"u": actor.user_id, "id": line_id})
        else:
            connection.execute(text("UPDATE recon_lines SET status = 'unmatched', payment_id = NULL, booking_id = NULL WHERE id = :id"), {"id": line_id})
            _mismatch(connection, "bank_credit_unmatched", line_id, f"{_money(line['amount_minor'])} on the statement: the suggested payment was rejected.",
                      line_id=line_id, actual=line["amount_minor"])
        finance.audit(connection, actor, "bank_match_confirmed" if accept else "bank_match_rejected", "recon_line", line_id, booking_id=line["booking_id"],
                      before={"status": "suggested", "payment_id": line["payment_id"]}, after={"status": "matched" if accept else "unmatched"})


# ---------------------------------------------------------------------------------------------------- 3. Suppliers
def check_supplier_charges(actor: finance.Actor) -> int:
    """Issued tickets with no supplier charge, or charges different from the stored cost. Returns mismatches added."""
    added = 0
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(text(
            "SELECT b.id, b.reference, b.supplier_cost_minor, b.cost_known, b.provider_id,"
            " (SELECT COALESCE(SUM(s.amount_minor), 0) FROM supplier_charges s WHERE s.booking_id = b.id)::bigint AS charged,"
            " EXISTS (SELECT 1 FROM supplier_charges s WHERE s.booking_id = b.id) AS has_charge"
            " FROM bookings b WHERE b.booking_status = 'ticket_issued' AND b.import_source IS NULL")).mappings().all()
        for row in rows:
            if not row["has_charge"]:
                added += _mismatch(connection, "ticket_without_charge", row["id"],
                                   f"{row['reference']} is ticketed but no supplier charge is recorded.", booking_id=row["id"],
                                   expected=row["supplier_cost_minor"] if row["cost_known"] else None)
            elif row["cost_known"] and row["charged"] != row["supplier_cost_minor"]:
                added += _mismatch(connection, "charge_differs_from_cost", f"{row['id']}:{row['charged']}",
                                   f"{row['reference']}: supplier charged {_money(row['charged'])}, the booking's cost is {_money(row['supplier_cost_minor'])}.",
                                   booking_id=row["id"], expected=row["supplier_cost_minor"], actual=row["charged"])
        finance.audit(connection, actor, "supplier_charges_checked", "reconciliation", None, after={"bookings": len(rows), "mismatches_added": added})
    return added


def import_supplier(provider_id: str, file_name: str, content: bytes, column_map: dict[str, Any], lines: list[Line], actor: finance.Actor) -> ImportResult:
    if not column_map.get("reference"):
        raise ReconRefused(422, "Choose the column with the PNR or booking reference.")
    matched = unmatched = added = 0
    with agencies.application_engine().begin() as connection:
        import_id = _start_import(connection, "supplier", None, provider_id, file_name, content, column_map, len(lines), actor)
        for line in lines:
            ref = (line.reference or "").strip()
            booking = connection.execute(text(
                "SELECT b.id, b.reference, (SELECT COALESCE(SUM(s.amount_minor), 0) FROM supplier_charges s WHERE s.booking_id = b.id)::bigint AS charged"
                " FROM bookings b WHERE upper(b.pnr) = upper(:r) OR upper(b.reference) = upper(:r) OR upper(b.supplier_response ->> 'final_pnr') = upper(:r)"
                " ORDER BY b.created_at DESC LIMIT 1"), {"r": ref}).mappings().one_or_none() if ref else None
            if booking is None:
                line_id = _insert_line(connection, import_id, line, "unmatched")
                unmatched += 1
                added += _mismatch(connection, "supplier_line_not_found", f"{provider_id}:{ref or line_id}",
                                   f"Supplier line {ref or '(no reference)'} for {_money(line.amount_minor)} matches no Travelian booking.",
                                   line_id=line_id, actual=line.amount_minor)
                continue
            line_id = _insert_line(connection, import_id, line, "matched", None, booking["id"], actor)
            matched += 1
            if abs(line.amount_minor) != booking["charged"]:
                added += _mismatch(connection, "supplier_statement_differs", f"{provider_id}:{booking['id']}:{line.amount_minor}",
                                   f"{booking['reference']}: the supplier's statement says {_money(abs(line.amount_minor))}; Travelian recorded {_money(booking['charged'])}.",
                                   line_id=line_id, booking_id=booking["id"], expected=booking["charged"], actual=abs(line.amount_minor))
    return ImportResult(import_id, len(lines), matched, 0, unmatched, added, 0)


# ---------------------------------------------------------------------------------------------------- lists
def list_imports(kind: str | None = None) -> list[dict]:
    with agencies.application_engine().begin() as connection:
        return [dict(r) for r in connection.execute(text(
            "SELECT i.id, i.kind, i.file_name, i.row_count, i.imported_at, i.imported_by_user_id, i.provider_id, a.label AS account_label,"
            " count(l.id) FILTER (WHERE l.status = 'matched') AS matched, count(l.id) FILTER (WHERE l.status = 'suggested') AS suggested,"
            " count(l.id) FILTER (WHERE l.status = 'unmatched') AS unmatched"
            " FROM recon_imports i LEFT JOIN payment_accounts a ON a.id = i.account_id LEFT JOIN recon_lines l ON l.import_id = i.id"
            + (" WHERE i.kind = :k" if kind else "") + " GROUP BY i.id, a.label ORDER BY i.imported_at DESC LIMIT 200"), {"k": kind}).mappings().all()]


def list_lines(import_id: str, status: str | None = None) -> list[dict]:
    with agencies.application_engine().begin() as connection:
        return [dict(r) for r in connection.execute(text(
            "SELECT l.id, l.line_no, l.reference, l.description, l.occurred_on, l.amount_minor, l.fee_minor, l.net_minor, l.payout_on, l.status,"
            " b.reference AS booking_reference, p.reference AS payment_reference, p.amount_minor AS payment_amount_minor"
            " FROM recon_lines l LEFT JOIN bookings b ON b.id = l.booking_id LEFT JOIN booking_payments p ON p.id = l.payment_id"
            " WHERE l.import_id = :i" + (" AND l.status = :s" if status else "") + " ORDER BY l.line_no LIMIT 2000"),
            {"i": import_id, "s": status}).mappings().all()]


def list_mismatches(status: str | None = "open", kind: str | None = None, limit: int = 200, offset: int = 0) -> tuple[list[dict], int]:
    where, params = ["true"], {"limit": limit, "offset": offset}
    if status:
        where.append("m.status = :s")
        params["s"] = status
    if kind:
        where.append("m.kind = :k")
        params["k"] = kind
    clause = " AND ".join(where)
    with agencies.application_engine().begin() as connection:
        total = connection.scalar(text(f"SELECT count(*) FROM recon_mismatches m WHERE {clause}"), params)
        rows = [dict(r) for r in connection.execute(text(
            "SELECT m.*, b.reference AS booking_reference FROM recon_mismatches m LEFT JOIN bookings b ON b.id = m.booking_id"
            f" WHERE {clause} ORDER BY m.created_at DESC LIMIT :limit OFFSET :offset"), params).mappings().all()]
    return rows, total


SUPPLIER_KINDS = frozenset({"ticket_without_charge", "charge_differs_from_cost", "supplier_line_not_found", "supplier_statement_differs"})


def resolve_mismatch(mismatch_id: str, actor: finance.Actor, status: str, reason: str, see_cost: bool = True) -> dict:
    if status not in ("explained", "resolved"):
        raise ReconRefused(422, "Mark it explained or resolved.")
    if not reason or not reason.strip():
        raise ReconRefused(422, "Say what happened, in plain words.")
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text("SELECT * FROM recon_mismatches WHERE id = :id FOR UPDATE"), {"id": mismatch_id}).mappings().one_or_none()
        if row is None:
            raise ReconRefused(404, "Mismatch not found.")
        if row["kind"] in SUPPLIER_KINDS and not see_cost:
            raise ReconRefused(403, "Supplier mismatches need the cost and margin permission.")
        if row["status"] == "resolved":
            raise ReconRefused(409, "This mismatch is already resolved.")
        if status == "explained":
            if row["status"] != "open":
                raise ReconRefused(409, "This mismatch is already explained. Resolve it when it's settled.")
            sets = "explanation = :r, explained_by_user_id = :u, explained_at = clock_timestamp()"
        else:
            sets = "resolution_reason = :r, resolved_by_user_id = :u, resolved_at = clock_timestamp()"
        connection.execute(text(f"UPDATE recon_mismatches SET status = :s, {sets} WHERE id = :id"),
                           {"s": status, "r": reason.strip(), "u": actor.user_id, "id": mismatch_id})
        finance.audit(connection, actor, f"mismatch_{status}", "recon_mismatch", mismatch_id, booking_id=row["booking_id"],
                      before={"status": row["status"], "kind": row["kind"]}, after={"status": status}, reason=reason.strip())
        return dict(connection.execute(text("SELECT m.*, b.reference AS booking_reference FROM recon_mismatches m LEFT JOIN bookings b ON b.id = m.booking_id"
                                            " WHERE m.id = :id"), {"id": mismatch_id}).mappings().one())
