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

Bank transfer and JazzCash never come here: staff record those payments and issue by hand (D49).
"""

from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import ISSUE_TICKETS, NONE, BackOfficeUser
from app.db import issuance as store
from app.services import issuance, notifications

logger = logging.getLogger(__name__)

#: The gateway itself issuing after a verified card payment: no user (audit entries carry no actor), no field visibility.
AUTO_ISSUER = BackOfficeUser(user_id=None, role="system", visibility=NONE, actions=frozenset({ISSUE_TICKETS}))  # type: ignore[arg-type]

_running: set[asyncio.Task] = set()


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


async def after_verified_card_payment(reference: str, client_factory=None) -> str:
    """Returns what happened: not_flight, pending (no issuance attempted), issued, or failed (staff alerted)."""
    target = await run_in_threadpool(store.load, reference)
    if target.product != "flight":
        return "not_flight"
    if target.paid_minor < target.total_minor:
        return "pending"  # defensive: only a fully paid booking is ever issued (store.blockers checks it again)
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
