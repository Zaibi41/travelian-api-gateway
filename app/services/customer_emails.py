"""Customer emails (approved templates: app/services/email_templates.py) and the signed links they carry.

When: the hold (card, Book Only, bank transfer, JazzCash), the verified payment, the issued ticket, a paid booking that
isn't ticketed yet, and a paid booking that can't be ticketed. Sent through SES to the booking's customer email when
NOTIFY_EMAIL_ENABLED is on. Every attempt is recorded on the booking (customer_email_sent / customer_email_not_sent with the
reason, e.g. SES sandbox refusing an unverified address), so the admin shows "Email not sent" and staff contact the
customer. Never raises: an email never changes or blocks a booking.

Signed link: a random token (only its SHA-256 is stored) that opens that one booking read-only on the website
(/my-bookings/view/<token>), expiring at the later of 30 days and 2 days after the last travel date.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.titles import title_text
from app.db import agencies
from app.db.public_bookings import new_token, token_hash
from app.db.supplier_bookings import _event
from app.services import baggage
from app.services import email_templates as t

logger = logging.getLogger(__name__)

KINDS = {"booking_held", "book_only_held", "bank_transfer", "jazzcash", "payment_received", "ticket_issued", "issuance_pending",
         "paid_not_ticketed", "hold_expired", "booking_cancelled"}
# The emails that tell a customer their seats are held: a hold that ends is only mentioned to customers who got one.
HOLD_KINDS = ("booking_held", "book_only_held", "bank_transfer", "jazzcash")
LINK_DAYS = 30


# ---------- signed links ----------

def _travel_end(itinerary: list | None) -> datetime | None:
    days = [str(leg.get("departure_local") or "")[:10] for leg in itinerary or [] if isinstance(leg, dict)]
    days = [day for day in days if len(day) == 10]
    if not days:
        return None
    try:
        return datetime.fromisoformat(max(days)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def link_expiry(now: datetime, itinerary: list | None) -> datetime:
    travel = _travel_end(itinerary)
    return max(now + timedelta(days=LINK_DAYS), travel + timedelta(days=2) if travel else now)


def create_link(booking_id: str, itinerary: list | None, purpose: str, now: datetime | None = None) -> str:
    """A new link token for one booking (only its hash is stored)."""
    token, hashed = new_token()
    now = now or datetime.now(timezone.utc)
    with agencies.application_engine().begin() as connection:
        connection.execute(text("INSERT INTO booking_view_links (id, booking_id, token_hash, purpose, expires_at) VALUES (:id, :b, :h, :p, :e)"),
                           {"id": str(uuid4()), "b": booking_id, "h": hashed, "p": purpose, "e": link_expiry(now, itinerary)})
    return token


def booking_for_link(token: str, now: datetime | None = None) -> str | None:
    """The booking reference a link opens, or None (unknown, malformed or expired). Marks the link used."""
    if not token or len(token) > 100:
        return None
    now = now or datetime.now(timezone.utc)
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "SELECT l.id, b.reference FROM booking_view_links l JOIN bookings b ON b.id = l.booking_id"
            " WHERE l.token_hash = :h AND l.expires_at > :now"), {"h": token_hash(token), "now": now}).one_or_none()
        if row is None:
            return None
        connection.execute(text("UPDATE booking_view_links SET last_used_at = :now WHERE id = :id"), {"now": now, "id": row.id})
    return row.reference


def view_url(token: str) -> str:
    return f"{settings.public_site_url.rstrip('/')}/my-bookings/view/{token}"


# ---------- facts ----------

def _facts(reference: str) -> dict[str, Any] | None:
    with agencies.application_engine().begin() as connection:
        row = connection.execute(text(
            "SELECT b.id, b.reference, b.pnr, b.hold_expires_at, b.currency, b.total_minor, b.itinerary, b.product,"
            " (SELECT COALESCE(SUM(p.amount_minor), 0) FROM booking_payments p WHERE p.booking_id = b.id) AS paid_minor,"
            " (SELECT COALESCE(SUM(p.card_fee_minor), 0) FROM booking_payments p WHERE p.booking_id = b.id)::bigint AS card_fee_minor,"
            " c.first_name AS c_first, c.last_name AS c_last, c.email AS c_email"
            " FROM bookings b LEFT JOIN booking_customers c ON c.booking_id = b.id WHERE b.reference = :r"), {"r": reference}).mappings().one_or_none()
        if row is None:
            return None
        travelers = connection.execute(text("SELECT title, first_name, last_name, ticket_number FROM booking_travelers WHERE booking_id = :b ORDER BY position"),
                                       {"b": row["id"]}).all()
    return {**dict(row), "travelers": [(" ".join(filter(None, (title_text(tr.title), tr.first_name, tr.last_name))), tr.ticket_number) for tr in travelers]}


def _when(local: str | None) -> str | None:
    if not local:
        return None
    try:
        moment = datetime.fromisoformat(str(local)[:16])
        return f"{moment:%d %b %Y}, {moment:%H:%M}"
    except ValueError:
        return str(local)[:16].replace("T", " ")


def baggage_of(legs: list[dict]) -> str | None:
    """The booking's baggage in full words: one line when every flight has the same allowance, else per flight."""
    shown = [(f"{leg.get('origin') or '?'}–{leg.get('destination') or '?'}", baggage.of_segment(leg).detail()) for leg in legs]
    if not shown:
        return None
    if len({words for _, words in shown}) == 1:
        return shown[0][1]
    return "; ".join(f"{route}: {words}" for route, words in shown)


def booking_from(facts: dict[str, Any], link: str | None) -> t.Booking:
    """The template's booking: no supplier, cost or internal fields (the template data has none)."""
    legs = [leg for leg in facts.get("itinerary") or [] if isinstance(leg, dict)]
    route = " → ".join([legs[0].get("origin") or "?"] + [leg.get("destination") or "?" for leg in legs]) if legs else None
    airline = legs[0].get("airline_name") if legs else None
    flight = " / ".join(filter(None, (leg.get("flight_number") for leg in legs))) or None
    customer = " ".join(filter(None, (facts.get("c_first"), facts.get("c_last")))) or None
    return t.Booking(reference=facts["reference"], passengers=[name for name, _ in facts["travelers"]], amount_minor=facts["total_minor"] or 0,
                     currency=facts["currency"] or "PKR", pnr=facts.get("pnr"), route=route, baggage=baggage_of(legs), travel_date=_when(legs[0].get("departure_local")) if legs else None,
                     airline=airline, flight=flight, hold_expires_at=facts.get("hold_expires_at"), paid_minor=facts.get("paid_minor") or None, card_fee_minor=int(facts.get("card_fee_minor") or 0),
                     tickets=[(name, number) for name, number in facts["travelers"] if number], customer_name=customer, view_url=link)


def _accounts(kind: str) -> list[t.Account]:
    from app.services.public_booking import bank_accounts, jazzcash_accounts

    views = bank_accounts() if kind == "bank_transfer" else jazzcash_accounts()
    return [t.Account(bank_name=v.bank_name, account_title=v.account_title, account_number=v.account_number, iban=v.iban, qr_url=v.qr_url)
            for v in views]


def render(kind: str, booking: t.Booking, *, method: str = "card") -> t.Email:
    if kind == "booking_held":
        return t.booking_held(booking)
    if kind == "book_only_held":
        return t.book_only_held(booking)
    if kind in ("bank_transfer", "jazzcash"):
        return t.transfer_instructions(booking, _accounts(kind), "bank" if kind == "bank_transfer" else "jazzcash")
    if kind == "payment_received":
        return t.payment_received(booking, method)
    if kind == "ticket_issued":
        return t.ticket_issued(booking)
    if kind == "issuance_pending":
        return t.issuance_pending(booking)
    if kind == "paid_not_ticketed":
        return t.paid_not_ticketed(booking)
    if kind == "hold_expired":
        return t.hold_expired(booking)
    if kind == "booking_cancelled":
        return t.booking_cancelled(booking)
    raise ValueError(kind)


# ---------- sending ----------

def ses_send(to: str, email: t.Email) -> tuple[bool, str]:
    """Send one HTML + text email; (sent, reason in words)."""
    if not settings.notify_email_enabled:
        return False, "skipped: NOTIFY_EMAIL_ENABLED is off"
    try:
        import boto3

        boto3.client("ses", region_name=settings.ses_region).send_email(
            Source=settings.notify_email_from, Destination={"ToAddresses": [to]}, ReplyToAddresses=[t.EMAIL],
            Message={"Subject": {"Data": email.subject[:200], "Charset": "UTF-8"},
                     "Body": {"Html": {"Data": email.html, "Charset": "UTF-8"}, "Text": {"Data": email.text, "Charset": "UTF-8"}}})
        return True, "sent"
    except Exception as error:  # noqa: BLE001 - never blocks a booking
        code = getattr(error, "response", {}).get("Error", {}).get("Code") if hasattr(error, "response") else None
        detail = (getattr(error, "response", {}).get("Error", {}).get("Message") or "") if hasattr(error, "response") else ""
        if code == "MessageRejected" and "not verified" in detail.lower():
            return False, "refused by the email service: this address isn't approved yet (until AWS approves full sending, only approved addresses receive email)"
        if code == "MessageRejected":
            return False, f"refused by SES (MessageRejected: {detail[:120]})"
        if code == "AccessDenied":
            # SES sandbox with a sending policy limited to verified identities: only those addresses may receive email.
            # SES's own message names the address and account, so it isn't kept.
            return False, ("refused by the email service (AccessDenied): Travelian's AWS sending permission doesn't cover this address. "
                           "Until AWS approves full sending, only approved addresses receive email")
        return False, f"failed ({code or type(error).__name__})"


def _record(booking_id: str, kind: str, sent: bool, reason: str, to: str | None) -> None:
    masked = None if not to else (to[0] + "***" + to[to.index("@"):] if "@" in to else "***")
    with agencies.application_engine().begin() as connection:
        _event(connection, booking_id, None, "customer_email_sent" if sent else "customer_email_not_sent",
               {"email": kind, "to": masked, **({} if sent else {"reason": reason})})


def _already_sent(booking_id: str, kind: str | tuple[str, ...]) -> bool:
    kinds = [kind] if isinstance(kind, str) else list(kind)
    with agencies.application_engine().begin() as connection:
        return bool(connection.scalar(text(
            "SELECT 1 FROM booking_events WHERE booking_id = :b AND action = 'customer_email_sent' AND details ->> 'email' = ANY(:k) LIMIT 1"),
            {"b": booking_id, "k": kinds}))


def deliver(reference: str, kind: str, *, method: str = "card") -> tuple[bool, str]:
    """Build and send one customer email for a booking (once per kind), and record the outcome on it. Never raises."""
    try:
        facts = _facts(reference)
        if facts is None:
            return False, "booking not found"
        if _already_sent(facts["id"], kind):
            return False, "already sent"
        if kind == "hold_expired" and (facts.get("paid_minor") or not _already_sent(facts["id"], HOLD_KINDS)):
            # Only an unpaid hold the customer was emailed about (a paid one follows the paid-not-ticketed path).
            return False, "not needed"
        to = (facts.get("c_email") or "").strip()
        if not to:
            _record(facts["id"], kind, False, "no customer email on the booking", None)
            return False, "no customer email on the booking"
        try:
            link = view_url(create_link(facts["id"], facts.get("itinerary"), kind))
        except Exception:  # noqa: BLE001 - no link (e.g. before the migration): the button falls back to My Bookings
            logger.warning("Booking link not created", extra={"reference": reference})
            link = None
        email = render(kind, booking_from(facts, link), method=method)
        sent, why = ses_send(to, email)
        _record(facts["id"], kind, sent, why, to)
        if not sent:
            logger.warning(f"Customer email not sent ({why})", extra={"reference": reference, "email_kind": kind})
        return sent, why
    except Exception as error:  # noqa: BLE001
        logger.error("Customer email failed", extra={"reference": reference, "email_kind": kind, "error_type": type(error).__name__})
        return False, f"failed ({type(error).__name__})"


async def send(reference: str, kind: str, *, method: str = "card") -> None:
    if kind not in KINDS:
        raise ValueError(kind)
    await run_in_threadpool(deliver, reference, kind, method=method)


def email_problems(events) -> list[dict[str, Any]]:
    """For the admin: customer emails whose last attempt wasn't sent (template, when, why)."""
    latest: dict[str, Any] = {}
    for event in events:
        if event.action in ("customer_email_sent", "customer_email_not_sent"):
            details = event.details if isinstance(event.details, dict) else json.loads(event.details or "{}")
            latest[details.get("email") or "?"] = (event, details)
    return [{"email": kind, "at": event.created_at, "reason": details.get("reason")}
            for kind, (event, details) in latest.items() if event.action == "customer_email_not_sent"]
