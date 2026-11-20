"""What follows a verified card payment on a website flight booking (D43, D45, D48, D49).

`after_verified_card_payment` has exactly one caller: app.services.public_booking.payfast_result, and only after
app.db.public_bookings.record_result recorded a PayFast result whose validation hash the gateway verified with its
secured key, for the amount the gateway asked for, and that payment made the booking fully paid. Nothing else (a
browser redirect, a staff-recorded bank transfer or JazzCash payment, a forged or unverified PayFast result) reaches it.

- Ticketing on for the booking's provider (app.services.issuance.ticketing_on): the gateway issues at once with the
  same safe issuance staff use (one ticketing call, never retried, PNR checked before and after). The real airline PNR
  and ticket numbers come only from the supplier; nothing is ever marked issued without them.
- Anything else (ticketing off, a supplier Travelian can't issue for yet, the hold already over, issuing refused or
  unclear) goes to app.services.flight_fallback.needs_staff: the hold is checked with the supplier, re-held once if it
  was lost, and admins get a red alert. The booking shows "Paid, issuance pending", or 'paid_not_ticketed' when the hold
  is lost for good.

Recovery (`recover`, every RECOVERY_INTERVAL_SECONDS from the app lifespan): a card-paid flight left not issued, or
stuck 'processing' (the gateway restarted mid-issue), for FLIGHT_ISSUANCE_STUCK_MINUTES since its last change gets ONE
automatic retry, claimed under a row lock (app.db.flight_recovery). A stuck 'processing' is never re-sent blind: the
gateway first asks the supplier (pnr_info, read-only) whether the ticket exists, and only issues if it shows none.

Bank transfer and JazzCash never come here: staff record those payments and issue by hand (D49).
"""

from __future__ import annotations

import asyncio
import logging

from datetime import datetime, timedelta, timezone

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.field_visibility import ISSUE_TICKETS, NONE, BackOfficeUser
from app.db import flight_recovery
from app.db import issuance as store
from app.services import issuance, notifications
from app.vendors.payments import payfast

logger = logging.getLogger(__name__)

#: The gateway itself issuing after a verified card payment: no user (audit entries carry no actor), no field visibility.
AUTO_ISSUER = BackOfficeUser(user_id=None, role="system", visibility=NONE, actions=frozenset({ISSUE_TICKETS}))  # type: ignore[arg-type]

_running: set[asyncio.Task] = set()
RECOVERY_INTERVAL_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: Suppliers issued automatically after a verified card payment. SooperFare is never issued automatically: staff issue it
#: from the admin (its confirm charges Travelian's SooperFare balance; gateway#45).
AUTO_ISSUING_PROVIDERS = (None, "southair")


def auto_issuing() -> bool:
    """Automatic issuing after a card payment: only with production PayFast (a UAT payment is test money) and Southair
    ticketing on, and only for AUTO_ISSUING_PROVIDERS."""
    return payfast.payments_allowed() and settings.southair_ticketing_enabled


def startup_summary() -> str:
    return f"PayFast mode: {payfast.mode()}, auto-issuing: {'on' if auto_issuing() else 'off'}"


def hold_over(target: store.Target, now: datetime) -> bool:
    """No time left to ticket: expired, or inside the ticketing margin (the same rule issuing itself applies)."""
    return target.hold_expires_at is None or (target.hold_expires_at - now).total_seconds() <= settings.ticketing_deadline_margin_seconds


def _route(itinerary) -> str | None:
    legs = [leg for leg in (itinerary or []) if isinstance(leg, dict)]
    if not legs:
        return None
    return " → ".join([legs[0].get("origin") or "?"] + [leg.get("destination") or "?" for leg in legs])


async def _alert(kind: str, target: store.Target, note: str, pnr: str | None) -> None:
    from app.db import booking_people

    people = await run_in_threadpool(booking_people.read, target.reference)
    lead = people.travelers[0] if people and people.travelers else None
    itinerary = await run_in_threadpool(_itinerary, target.reference)
    await notifications.booking_event(
        kind, reference=target.reference, passenger=" ".join(filter(None, (lead.title, lead.first_name, lead.last_name))) if lead else None,
        route=_route(itinerary), fare_minor=target.total_minor, currency=target.currency, pnr=pnr, hold_expires_at=target.hold_expires_at,
        note=note)


def _itinerary(reference: str):
    from sqlalchemy import text

    from app.db import agencies

    with agencies.application_engine().begin() as connection:
        return connection.scalar(text("SELECT itinerary FROM bookings WHERE reference = :r"), {"r": reference})


async def after_verified_card_payment(reference: str, client_factory=None, now: datetime | None = None, sooperfare_factory=None) -> str:
    """Returns: not_flight, pending (not fully paid: nothing done), issued, needs_staff (red alert; the admin shows "Paid,
    issuance pending") or paid_not_ticketed (hold lost and re-holding failed; red alert)."""
    from app.services import flight_fallback

    from app.services import customer_emails

    now = now or _now()
    target = await run_in_threadpool(store.load, reference)
    if target.paid_minor >= target.total_minor:
        await customer_emails.send(reference, "payment_received", method="card")
    if target.product != "flight":
        return "not_flight"
    if target.paid_minor < target.total_minor:
        return "pending"  # defensive: only a fully paid booking is ever issued (store.blockers checks it again)

    async def staff(reason: str) -> str:
        return await flight_fallback.needs_staff(reference, reason, now=now, client_factory=client_factory, sooperfare_factory=sooperfare_factory)

    if hold_over(target, now):
        # Paid after the airline hold ended (or too close to it to ticket): never sent to the supplier for ticketing.
        return await staff("the card payment arrived after the airline hold ended (or too close to it to issue automatically)")
    if not payfast.payments_allowed():
        return await staff(f"automatic issuing is off: PayFast is in {payfast.mode()} mode, so nothing was sent to the supplier")
    if target.provider not in AUTO_ISSUING_PROVIDERS:
        return await staff("this supplier's tickets are issued by staff from the admin (Payments tab, Issue the ticket), never automatically")
    reason = issuance.ticketing_reason(target.provider, target.product)
    if reason:
        return await staff(f"automatic issuing didn't run. {reason}")
    try:
        outcome = await issuance.issue(reference, AUTO_ISSUER, client_factory=client_factory)
    except store.IssuanceRefused as error:
        return await staff(f"automatic issuing didn't go ahead: {error.message}")
    except Exception as error:  # noqa: BLE001 - the payment is recorded; staff must finish the issuance
        logger.error("Automatic issuance failed unexpectedly", extra={"reference": reference, "error_type": type(error).__name__})
        return await staff("automatic issuing failed unexpectedly: check the issuance status with the supplier first")
    if outcome.result == "issued":
        await customer_emails.send(reference, "ticket_issued")
        await _alert("auto_issued", target, f"Card payment verified by PayFast; tickets issued automatically ({len(outcome.tickets)}).", target.pnr)
        return "issued"
    return await staff(f"automatic issuing didn't issue the ticket: {outcome.message}")


async def recover(now: datetime | None = None, client_factory=None, sooperfare_factory=None) -> dict[str, str]:
    """One recovery pass. Returns {reference: outcome} for logs and tests. Also sends the 30-minute red alerts."""
    from app.services import flight_fallback

    now = now or _now()
    minutes = settings.flight_issuance_stuck_minutes
    before = now - timedelta(minutes=minutes)
    outcomes: dict[str, str] = {}
    for reference, status, provider in await run_in_threadpool(flight_recovery.stuck_card_payments, before):
        if status == "not_issued" and not (auto_issuing() and provider in AUTO_ISSUING_PROVIDERS and issuance.ticketing_on(provider)):
            continue  # paid, issuance pending with staff by design (already alerted); nothing to retry
        claimed = await run_in_threadpool(flight_recovery.claim_recovery, reference, before, {"stuck_minutes": minutes})
        if claimed is None:
            continue  # another worker has it, it changed, or it was already retried once
        if claimed == "processing":
            outcomes[reference] = await _settle_stuck(reference, client_factory)
            if outcomes[reference] == "unknown":
                outcomes[reference] = await flight_fallback.needs_staff(
                    reference, "issuing was interrupted (gateway restart) and the supplier's answer is unclear: the issuance is 'unknown', "
                    "verify it with the supplier before issuing", now=now, client_factory=client_factory, sooperfare_factory=sooperfare_factory)
            if outcomes[reference] != "not_issued":
                continue
        outcomes[reference] = await after_verified_card_payment(reference, client_factory, now, sooperfare_factory)
        logger.info("Stuck flight issuance recovered", extra={"reference": reference, "outcome": outcomes[reference]})
    for reference in await flight_fallback.thirty_minute_alerts(now):
        outcomes.setdefault(reference, "red_alert_30m")
    return outcomes


async def _settle_stuck(reference: str, client_factory) -> str:
    """'processing' with no live attempt (the gateway restarted mid-issue): a ticketing call may have reached the supplier,
    so never re-send it blind. Mark it unknown, ask the supplier (read-only), and settle it from the answer."""
    target = await run_in_threadpool(store.load, reference)
    try:
        await run_in_threadpool(store.release, target, None, "unknown", "issuance_unknown", {"reason_code": "stuck_processing"})
        outcome = await issuance.verify(reference, AUTO_ISSUER, client_factory=client_factory)
    except Exception as error:  # noqa: BLE001 - left unknown; staff verify it
        logger.warning("Stuck issuance not settled", extra={"reference": reference, "error_type": type(error).__name__})
        outcome = None
    if outcome is not None and outcome.result == "issued":
        await _alert("auto_issued", target, "Issuing was interrupted (gateway restart); the supplier shows the ticket issued, and the "
                     "ticket numbers are now recorded.", target.pnr)
        return "issued"
    if outcome is not None and outcome.result == "not_issued":
        return "not_issued"
    return "unknown"


async def run_forever(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await recover()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the job must outlive one bad pass
            logger.warning("Flight issuance recovery pass failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=RECOVERY_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


def start_after_verified_card_payment(reference: str) -> None:
    """Run it in the background so PayFast's notification (and the customer's return) are answered at once."""
    task = asyncio.get_running_loop().create_task(_safe(reference))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _safe(reference: str) -> None:
    try:
        await after_verified_card_payment(reference)
    except Exception as error:  # noqa: BLE001 - never lose the error silently
        logger.error("After-payment step failed", extra={"reference": reference, "error_type": type(error).__name__})
