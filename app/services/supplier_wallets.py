"""Supplier balances (travelian#68): read Southair's balance (read-only /my-profile), check thresholds, alert once.

Southair is read on a schedule (every BALANCE_INTERVAL_SECONDS) and when staff press Refresh. SooperFare has no balance
API: its balance is calculated from recorded top-ups, adjustments and ticket charges. No supplier call here changes
anything at the supplier.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from starlette.concurrency import run_in_threadpool

from app.db import finance, supplier_wallets as store

logger = logging.getLogger(__name__)
BALANCE_INTERVAL_SECONDS = 30 * 60


class BalanceUnavailable(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def read_southair(client_factory=None) -> tuple[int, str]:
    """Southair's current balance (limit.cap) in paisa. Read-only. Raises BalanceUnavailable in plain words."""
    from app.vendors.flights.southair import normalize
    from app.vendors.flights.southair.client import SouthairError, configured_client

    try:
        profile = await (client_factory or configured_client)().profile()
    except SouthairError:
        raise BalanceUnavailable("Southair didn't answer. The last known balance is shown.") from None
    amount = normalize.parse_amount(((profile or {}).get("limit") or {}).get("cap"))
    if amount is None:
        raise BalanceUnavailable("Southair's answer had no balance. The last known balance is shown.")
    minor = int((Decimal(str(amount.value)) * 100).quantize(Decimal("1"), ROUND_HALF_UP))
    currency = (getattr(amount, "currency", None) or "PKR").upper()
    return minor, currency if len(currency) == 3 else "PKR"


async def refresh_southair(actor: finance.Actor, client_factory=None) -> store.Wallet:
    minor, currency = await read_southair(client_factory)
    await run_in_threadpool(store.record_snapshot, "southair", minor, currency, actor)
    return await check_low("southair")


async def check_low(provider: str) -> store.Wallet:
    """After any change: alert once when the balance drops below its threshold (Notifications + email)."""
    wallet = await run_in_threadpool(store.wallet, provider)
    if wallet.low is None:
        return wallet
    if await run_in_threadpool(store.alert_state, provider, bool(wallet.low), _now()):
        await _low_balance_alert(wallet)
    return wallet


async def _low_balance_alert(wallet: store.Wallet) -> None:
    """Notifications (always) and email (when set up). Never raises."""
    from app.services import notifications

    # Notifications are seen by all back-office staff: no supplier name or amounts here (field visibility). Finance
    # opens Finance > Wallets for the figures.
    title = "Low supplier balance: check Finance > Wallets"
    lines = ["A supplier balance dropped below its alert level.", "Top up before issuing more tickets (Finance > Wallets)."]
    try:
        notification_id = await run_in_threadpool(notifications._record, "low_supplier_balance", None, title,
                                                   {"provider_id": wallet.provider_id, "urgent": True})
    except Exception:  # noqa: BLE001
        logger.error("Low balance alert not recorded", exc_info=True)
        return
    if await run_in_threadpool(notifications._send_email, title, "\n".join(lines)):
        try:
            await run_in_threadpool(notifications._mark_emailed, notification_id)
        except Exception:  # noqa: BLE001
            pass


def issue_warning(provider: str | None, cost_minor: int | None, show_amounts: bool) -> str | None:
    """The Issue panel's warning when this supplier's known balance is low or below the ticket's cost."""
    provider = provider or "southair"
    if provider not in store.SUPPLIERS:
        return None
    wallet = store.wallet(provider)
    if wallet.balance_minor is None:
        return None
    short = cost_minor is not None and wallet.balance_minor < cost_minor
    if not (short or wallet.low):
        return None
    from app.services.notifications import _money

    known = "calculated" if wallet.mode == "calculated" else "last checked"
    if not show_amounts:
        return "The supplier balance may not cover this ticket. Check Finance > Wallets before issuing."
    if short:
        return (f"{wallet.name} balance ({known}) is {_money(wallet.balance_minor, wallet.currency)}, less than this ticket's "
                f"{_money(cost_minor, wallet.currency)}. Top up before issuing.")
    return f"{wallet.name} balance ({known}) is low: {_money(wallet.balance_minor, wallet.currency)}. Top up soon."


async def run_forever(stop: asyncio.Event) -> None:
    """Scheduled Southair balance check (read-only). Skipped quietly when Southair isn't configured."""
    from app.core.config import settings

    while not stop.is_set():
        if settings.southair_base_url and settings.southair_username:
            try:
                await refresh_southair(finance.SYSTEM)
            except asyncio.CancelledError:
                raise
            except BalanceUnavailable as error:
                logger.warning("Scheduled Southair balance check: %s", error.message)
            except Exception:  # noqa: BLE001 - the job must outlive one bad pass
                logger.warning("Scheduled Southair balance check failed", exc_info=True)
        try:  # SooperFare: calculated from records; tickets issued since the last pass may have taken it below its alert
            await check_low("sooperfare")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("Scheduled SooperFare balance check failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=BALANCE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
