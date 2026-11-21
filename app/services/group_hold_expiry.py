"""Hold expiry job (started from the app lifespan): Group Ticketing holds and flight holds.

Every GROUP_HOLD_JOB_INTERVAL_SECONDS:
1. Held group bookings past Travelian's own time limit with no money recorded become 'expired' (logged), with their
   seats marked for release. With money recorded they never become plain 'expired' (gateway#109): paid in full, they
   stay held, waiting for the final PNR (ticketing status "Paid – awaiting final PNR"; staff record it); part-paid, they
   become 'paid_not_ticketed' (seats marked for release, staff alerted to refund or rebook).
2. Every booking whose seats still need releasing (expired by time, or a staff release the
   supplier didn't confirm) gets one release call. Success: logged as supplier_released. Failure:
   logged, attempts + 1, tried again next run, 'failed' after GROUP_HOLD_RELEASE_ATTEMPTS. While a
   failed attempt stands, Admin and Super Admin see "Supplier release failed, release manually".
Release is the only supplier call this job makes; sources without a release call (Dhillu & Sahi) get none:
the supplier cancels their holds by itself. Nothing here depends on a supplier expiry.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.db import flight_expiry
from app.db import group_holds as store
from app.services.group_holds import NO_RELEASE_SOURCES, HeldRef, paid_not_ticketed_alert, provider_for

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def run_once(now: datetime | None = None) -> dict[str, list[str]]:
    """One pass. Returns what happened: expired, released, failed (references), for logs and tests."""
    now = now or _now()
    outcome: dict[str, list[str]] = {"expired": [], "paid_not_ticketed": [], "released": [], "failed": [], "flights_expired": []}
    # Flights: held past the time limit and not issued -> expired. No supplier call: the hold lapses at the airline.
    flights = await run_in_threadpool(flight_expiry.expire_due, now)
    outcome["flights_expired"] = [item.reference for item in flights]
    logger.info("Flight holds expired", extra={"count": len(flights)})
    from app.services import customer_emails

    for item in flights:
        if not item.paid_minor:
            await customer_emails.send(item.reference, "hold_expired")  # only if the customer was told about the hold
        if item.paid_minor:
            logger.warning("Flight hold ended with a payment recorded: paid, not ticketed (refund or rebook by hand)",
                           extra={"reference": item.reference, "amount_minor": item.paid_minor, "currency": item.currency})
            from app.services import flight_fallback

            await flight_fallback.hold_lost(item.reference, "the airline hold expired with money paid and no ticket")
    expired = await run_in_threadpool(store.expire_due, now, frozenset(NO_RELEASE_SOURCES))
    outcome["expired"] = expired
    for reference in expired:
        logger.info("Group hold expired", extra={"reference": reference})
        await customer_emails.send(reference, "hold_expired")  # unpaid holds the customer was told about
    part_paid = await run_in_threadpool(store.paid_not_ticketed_due, now, frozenset(NO_RELEASE_SOURCES))
    outcome["paid_not_ticketed"] = [booking.reference for booking in part_paid]
    for booking in part_paid:
        await paid_not_ticketed_alert(booking, "The group hold time passed")
    due = await run_in_threadpool(store.releases_due, settings.group_hold_release_attempts)
    for booking in due:
        provider = provider_for(booking.provider)
        error = None
        if provider is None or not booking.supplier_ref:
            error = "no_provider"
        else:
            try:
                await provider.release(HeldRef(booking.pnr, booking.supplier_ref, booking.adults))
            except Exception as exc:  # noqa: BLE001 - recorded and retried next run
                error = getattr(exc, "error_code", type(exc).__name__)
        updated = await run_in_threadpool(store.mark_release_attempt, booking.reference, None, error is None, error, now,
                                          settings.group_hold_release_attempts)
        if error is None:
            if booking.booking_status == store.HOLD_UNRECORDED:
                await run_in_threadpool(store.mark_unrecorded_released, booking.reference, None)
            outcome["released"].append(booking.reference)
            logger.info("Group hold seats released at the supplier", extra={"reference": booking.reference})
        else:
            outcome["failed"].append(booking.reference)
            logger.warning("Group hold release failed", extra={"reference": booking.reference, "error_code": error,
                                                               "attempts": updated.release.get("attempts"), "status": updated.release.get("status")})
    return outcome


async def run_forever(stop: asyncio.Event) -> None:
    """Run until `stop` is set. A failing pass (database down, etc.) is logged and tried again."""
    while not stop.is_set():
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the job must outlive one bad pass
            logger.warning("Group hold expiry pass failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.group_hold_job_interval_seconds)
        except asyncio.TimeoutError:
            continue
