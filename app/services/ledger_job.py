"""Posts ledger journals for new events every 10 minutes (travelian#72). Idempotent; never changes a money record."""

from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 10 * 60


async def run_forever(stop: asyncio.Event) -> None:
    from app.db import ledger

    while not stop.is_set():
        try:
            await run_in_threadpool(ledger.post_pending)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the job must outlive one bad pass
            logger.warning("Ledger posting pass failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
