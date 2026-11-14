"""Group hold expiry job (started from the app lifespan).

Every GROUP_HOLD_JOB_INTERVAL_SECONDS:
1. Held group bookings past Travelian's own time limit become 'expired' (logged), with their
   seats marked for release.
2. Every booking whose seats still need releasing (expired by time, or a staff release the
   supplier didn't confirm) gets one release call. Success: logged as supplier_released. Failure:
   logged, attempts + 1, tried again next run, 'failed' after GROUP_HOLD_RELEASE_ATTEMPTS. While a
   failed attempt stands, Admin and Super Admin see "Supplier release failed, release manually".
Release is the only supplier call this job makes. Nothing here depends on a supplier expiry.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.db import group_holds as store
from app.services.group_holds import HeldRef, provider_for

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def run_once(now: datetime | None = None) -> dict[str, list[str]]:
    """One pass. Returns what happened: expired, released, failed (references), for logs and tests."""
    now = now or _now()
    outcome: dict[str, list[str]] = {"expired": [], "released": [], "failed": []}
    expired = await run_in_threadpool(store.expire_due, now)
    outcome["expired"] = expired
    for reference in expired:
        logger.info("Group hold expired", extra={"reference": reference})
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
