"""Staff notifications (B1): a hold, a booking request, a restricted-airline request, or a card payment on a website
flight (paid and pending issuance, issued automatically, or automatic issuance failed).

Every notification is written to admin_notifications (shown in the admin) in its own transaction, after the booking is
saved. Email goes through Amazon SES when NOTIFY_EMAIL_ENABLED is on and the server has AWS credentials; an email
failure is logged and never changes the booking. No passenger documents, dates of birth or contact details beyond the
lead passenger's name are included.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.db import agencies

logger = logging.getLogger(__name__)

KINDS = {"hold_created", "booking_requested", "restricted_request", "bank_transfer_hold", "paid_issuance_pending", "auto_issued",
         "auto_issue_failed", "paid_not_ticketed"}


def _money(amount_minor: int | None, currency: str | None) -> str:
    if amount_minor is None or not currency:
        return "—"
    label = "Rs" if currency == "PKR" else currency
    whole, paisa = divmod(amount_minor, 100)
    return f"{label} {whole:,}" + (f".{paisa:02d}" if paisa else "")


def _record(kind: str, reference: str | None, title: str, details: dict[str, Any]) -> str:
    notification_id = str(uuid4())
    with agencies.application_engine().begin() as connection:
        booking_id = connection.scalar(text("SELECT id FROM bookings WHERE reference = :r"), {"r": reference}) if reference else None
        connection.execute(
            text("INSERT INTO admin_notifications (id, kind, booking_id, reference, title, details) VALUES (:id, :k, :b, :r, :t, CAST(:d AS jsonb))"),
            {"id": notification_id, "k": kind, "b": booking_id, "r": reference, "t": title[:200], "d": json.dumps(details, default=str)},
        )
    return notification_id


def _mark_emailed(notification_id: str) -> None:
    with agencies.application_engine().begin() as connection:
        connection.execute(text("UPDATE admin_notifications SET emailed = true WHERE id = :id"), {"id": notification_id})


def recipients() -> list[str]:
    return [address.strip() for address in settings.notify_emails.split(",") if "@" in address]


def email_configured() -> bool:
    """Email alerts are switched on and have someone to go to (SES itself is only known when a send is tried)."""
    return bool(settings.notify_email_enabled and recipients())


def _send_email(subject: str, body: str) -> bool:
    if not settings.notify_email_enabled or not recipients():
        return False
    try:
        import boto3

        boto3.client("ses", region_name=settings.ses_region).send_email(
            Source=settings.notify_email_from,
            Destination={"ToAddresses": recipients()},
            Message={"Subject": {"Data": subject[:200]}, "Body": {"Text": {"Data": body}}},
        )
        return True
    except Exception as error:  # noqa: BLE001 - email never blocks or changes a booking
        logger.warning("Staff notification email not sent", extra={"error_type": type(error).__name__})
        return False


async def booking_event(kind: str, *, reference: str, passenger: str | None, route: str | None, fare_minor: int | None,
                        currency: str | None, pnr: str | None, hold_expires_at: datetime | None, note: str | None = None) -> None:
    """Record and email one staff notification. Never raises: the booking it reports is already saved."""
    titles = {
        "hold_created": "Seats held",
        "booking_requested": "New booking request",
        "restricted_request": "Request to book (restricted airline): no supplier hold",
        "bank_transfer_hold": "Held for bank transfer: payment pending",
        "paid_issuance_pending": "Paid by card: issuance pending",
        "auto_issued": "Paid by card: ticket issued automatically",
        "auto_issue_failed": "Paid by card: automatic issuance failed, issue by hand",
        "paid_not_ticketed": "Paid, not ticketed: refund or rebook",
    }
    title = f"{titles.get(kind, 'Booking')}: {reference}"
    details = {"reference": reference, "passenger": passenger, "route": route, "fare": _money(fare_minor, currency),
               "pnr": pnr, "hold_expires_at": hold_expires_at.isoformat() if hold_expires_at else None, "note": note}
    try:
        notification_id = await run_in_threadpool(_record, kind, reference, title, details)
    except Exception as error:  # noqa: BLE001
        logger.error("Staff notification not recorded", extra={"reference": reference, "error_type": type(error).__name__})
        return
    lines = [f"Booking reference: {reference}", f"Passenger: {passenger or '—'}", f"Route: {route or '—'}",
             f"Fare: {details['fare']}", f"PNR: {pnr or '— (no supplier hold)'}",
             f"Hold expires: {hold_expires_at.strftime('%d %b %Y %H:%M UTC') if hold_expires_at else '—'}"]
    if note:
        lines.append(note)
    if await run_in_threadpool(_send_email, f"Travelian: {title}", "\n".join(lines)):
        try:
            await run_in_threadpool(_mark_emailed, notification_id)
        except Exception:  # noqa: BLE001
            pass


def recent(limit: int = 50) -> list[dict[str, Any]]:
    with agencies.application_engine().begin() as connection:
        rows = connection.execute(
            text("SELECT id, kind, reference, title, details, emailed, created_at FROM admin_notifications ORDER BY created_at DESC LIMIT :l"),
            {"l": max(1, min(limit, 200))},
        ).mappings().all()
    return [dict(row) for row in rows]
