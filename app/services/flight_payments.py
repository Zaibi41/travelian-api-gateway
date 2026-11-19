"""What follows a verified card payment on a website flight booking (D43, D45, D48, D49).

`after_verified_card_payment` has exactly one caller: app.services.public_booking.payfast_result, and only after
app.db.public_bookings.record_result recorded a PayFast result whose validation hash the gateway verified with its
secured key, for the amount the gateway asked for, and that payment made the booking fully paid. Nothing else (a
browser redirect, a staff-recorded bank transfer or JazzCash payment, a forged or unverified PayFast result) reaches it.

- Ticketing on for the booking's provider (app.services.issuance.ticketing_on): the gateway issues at once with the
  same safe issuance staff use (one ticketing call, never retried, PNR checked before and after). The real airline
  PNR and ticket numbers come only from the supplier. Anything short of issued (refused, not issued, unknown) leaves
  the booking paid and not issued, and staff are alerted to finish it; the customer sees "payment received, issuance
  in progress". Nothing is ever marked issued without the supplier's ticket numbers.
- Ticketing off, or a provider the gateway can't issue for (SooperFare today): nothing is sent to the supplier. The
  booking stays paid and not issued, the customer sees "payment received, issuance pending", and staff are alerted.

- The hold has expired (or is inside the ticketing margin) when the money arrives: nothing is sent to the supplier; the
  booking becomes 'paid_not_ticketed' (refund or rebook by hand) and staff are alerted. The same happens when issuing
  fails for good (the supplier won't ticket the PNR, or the airline's price is now above the selling price).

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

logger = logging.getLogger(__name__)

#: The gateway itself issuing after a verified card payment: no user (audit entries carry no actor), no field visibility.
AUTO_ISSUER = BackOfficeUser(user_id=None, role="system", visibility=NONE, actions=frozenset({ISSUE_TICKETS}))  # type: ignore[arg-type]

_running: set[asyncio.Task] = set()
#: Issuance refusals that won't change by trying again: the booking can't be ticketed as it is.
PERMANENT_REFUSALS = ("not_ticketable", "below_cost")
RECOVERY_INTERVAL_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hold_over(target: store.Target, now: datetime) -> bool:
    """No time left to ticket: expired, or inside the ticketing margin (the same rule issuing itself applies)."""
    return target.hold_expires_at is None or (target.hold_expires_at - now).total_seconds() <= settings.ticketing_deadline_margin_seconds


async def _not_ticketed(target: store.Target, reason: str, note: str) -> str:
    if await run_in_threadpool(flight_recovery.mark_paid_not_ticketed, target.reference, reason, _now()):
        await _alert("paid_not_ticketed", target, f"{note} The customer paid and has no ticket: refund them or rebook a new fare. "
                     "Nothing was refunded automatically.", target.pnr)
        return "paid_not_ticketed"
    return "failed"


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


async def after_verified_card_payment(reference: str, client_factory=None, now: datetime | None = None) -> str:
    """Returns what happened: not_flight, pending (no issuance attempted), issued, paid_not_ticketed, or failed (staff
    alerted)."""
    target = await run_in_threadpool(store.load, reference)
    if target.product != "flight":
        return "not_flight"
    if target.paid_minor < target.total_minor:
        return "pending"  # defensive: only a fully paid booking is ever issued (store.blockers checks it again)
    if hold_over(target, now or _now()):
        # Paid after the airline hold ended (or too close to it to ticket): never sent to the supplier.
        return await _not_ticketed(target, "paid_after_hold_expired",
                                   "A verified card payment arrived after the airline hold expired, so nothing was sent to the supplier.")
    if not issuance.ticketing_on(target.provider):
        reason = ("This fare's supplier isn't issued from Travelian yet: issue it in the supplier's portal, then record the ticket numbers."
                  if (target.provider or "southair") not in issuance.ISSUING_PROVIDERS else
                  "Automatic issuance is switched off (ticketing flag off), so nothing was sent to the supplier.")
        await _alert("paid_issuance_pending", target, f"Card payment verified by PayFast. {reason} The customer sees "
                     "'Payment received, issuance pending'. Issue before the hold expires.", target.pnr)
        return "pending"
    try:
        outcome = await issuance.issue(reference, AUTO_ISSUER, client_factory=client_factory)
    except store.IssuanceRefused as error:
        if error.code in PERMANENT_REFUSALS or hold_over(await run_in_threadpool(store.load, reference), _now()):
            return await _not_ticketed(target, error.code or "hold_expired", f"Automatic issuance after the card payment failed for good: {error.message}")
        await _alert("auto_issue_failed", target, f"Card payment verified by PayFast, but automatic issuance didn't go ahead: {error.message} "
                     "The customer sees 'Payment received, issuance in progress'. Check the booking and issue it by hand.", target.pnr)
        return "failed"
    except Exception as error:  # noqa: BLE001 - the payment is recorded; staff must finish the issuance
        logger.error("Automatic issuance failed unexpectedly", extra={"reference": reference, "error_type": type(error).__name__})
        await _alert("auto_issue_failed", target, "Card payment verified by PayFast, but automatic issuance failed unexpectedly. "
                     "Check the booking's issuance status with the supplier before issuing by hand.", target.pnr)
        return "failed"
    if outcome.result == "issued":
        await _alert("auto_issued", target, f"Card payment verified by PayFast; tickets issued automatically ({len(outcome.tickets)}).", target.pnr)
        return "issued"
    await _alert("auto_issue_failed", target, f"Card payment verified by PayFast, but the ticket wasn't issued automatically: {outcome.message} "
                 "The customer sees 'Payment received, issuance in progress'.", target.pnr)
    return "failed"


async def recover(now: datetime | None = None, client_factory=None) -> dict[str, str]:
    """One recovery pass. Returns {reference: outcome} for logs and tests."""
    now = now or _now()
    minutes = settings.flight_issuance_stuck_minutes
    before = now - timedelta(minutes=minutes)
    outcomes: dict[str, str] = {}
    for reference, status, provider in await run_in_threadpool(flight_recovery.stuck_card_payments, before):
        if status == "not_issued" and not issuance.ticketing_on(provider):
            continue  # paid, issuance pending with staff by design (already alerted); nothing to retry
        claimed = await run_in_threadpool(flight_recovery.claim_recovery, reference, before, {"stuck_minutes": minutes})
        if claimed is None:
            continue  # another worker has it, it changed, or it was already retried once
        if claimed == "processing":
            outcomes[reference] = await _settle_stuck(reference, client_factory)
            if outcomes[reference] != "not_issued":
                continue
        outcomes[reference] = await after_verified_card_payment(reference, client_factory, now)
        logger.info("Stuck flight issuance recovered", extra={"reference": reference, "outcome": outcomes[reference]})
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
    await _alert("auto_issue_failed", target, "Issuing was interrupted (gateway restart) and the supplier's answer is unclear. The "
                 "issuance is 'unknown': verify it with the supplier before issuing by hand.", target.pnr)
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
