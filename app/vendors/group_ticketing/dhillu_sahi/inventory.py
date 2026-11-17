"""Dhillu & Sahi group inventory: one listing call (POST /api/pnr-respect-to-mysector-user, JSON {"my_sector_id": "<id>"})
per sector group id in DHILLU_SAHI_SECTOR_GROUP_IDS, merged. The ids come only from the environment; unset or empty
means the listing is not configured and is never called. A ticket
listed under more than one id is kept once (by supplier ticket id). One failing id is logged and the others are still
returned; only when every id fails is the supplier reported as failed."""

import asyncio
import logging

from app.core.config import settings
from app.vendors.group_ticketing.dhillu_sahi.auth import DhilluSahiAuthClient, DhilluSahiAuthError
from app.vendors.group_ticketing.dhillu_sahi.groups import GroupInventory, normalize_groups

logger = logging.getLogger(__name__)


MISSING_IDS_WARNING = ("Dhillu & Sahi listing not configured: DHILLU_SAHI_SECTOR_GROUP_IDS is not set. "
                       "Dhillu & Sahi fares are left out of Group Ticketing until it is set (e.g. DHILLU_SAHI_SECTOR_GROUP_IDS=<id>,<id>).")


def sector_group_ids() -> list[str]:
    """DHILLU_SAHI_SECTOR_GROUP_IDS as written: comma-separated, each item trimmed, empty items and repeats dropped."""
    values = (settings.dhillu_sahi_sector_group_ids or "").split(",")
    ids: list[str] = []
    for value in (value.strip() for value in values):
        if value and value not in ids:
            ids.append(value)
    return ids


def is_configured() -> bool:
    return bool(settings.dhillu_sahi_email and settings.dhillu_sahi_password and sector_group_ids())


def warn_if_ids_missing() -> bool:
    """At startup: one warning naming the missing setting. Returns whether it warned."""
    if sector_group_ids():
        return False
    logger.warning(MISSING_IDS_WARNING, extra={"setting": "DHILLU_SAHI_SECTOR_GROUP_IDS"})
    return True


def merge(listings: list[tuple[str, list[GroupInventory]]]) -> list[GroupInventory]:
    """Groups from every sector group id; a ticket already seen (same supplier ticket id) is dropped, and a group
    left without tickets is dropped. Tickets without a supplier ticket id are all kept."""
    seen: set[str] = set()
    merged = []
    for _, groups in listings:
        for group in groups:
            tickets = []
            for ticket in group.tickets:
                if ticket.supplier_ticket_id is not None:
                    if ticket.supplier_ticket_id in seen:
                        continue
                    seen.add(ticket.supplier_ticket_id)
                tickets.append(ticket)
            if tickets:
                merged.append(group.model_copy(update={"tickets": tickets}))
    return merged


async def fetch_inventory(client: DhilluSahiAuthClient | None = None) -> list[GroupInventory]:
    """All group inventory for the configured sector group ids, merged."""
    if not is_configured():
        raise RuntimeError("Dhillu & Sahi credentials are not configured")
    client = client or DhilluSahiAuthClient(settings.dhillu_sahi_base_url, timeout_seconds=settings.dhillu_sahi_timeout_seconds)
    password = settings.dhillu_sahi_password
    login = await client.login(settings.dhillu_sahi_email or "", password.get_secret_value() if password else "")
    ids = sector_group_ids()
    answers = await asyncio.gather(*(client.fetch_groups(login["token"], sector_id) for sector_id in ids), return_exceptions=True)
    listings: list[tuple[str, list[GroupInventory]]] = []
    failures: list[BaseException] = []
    for sector_id, answer in zip(ids, answers):
        if isinstance(answer, BaseException):
            failures.append(answer)
            logger.warning("Dhillu & Sahi listing failed for one sector group",
                           extra={"sector_group_id": sector_id, "error_code": getattr(answer, "error_code", type(answer).__name__)})
            continue
        try:
            listings.append((sector_id, normalize_groups(answer, scope=sector_id)))
        except Exception as error:  # noqa: BLE001 - a malformed listing for one id must not hide the others
            failures.append(error)
            logger.warning("Dhillu & Sahi listing unreadable for one sector group",
                           extra={"sector_group_id": sector_id, "error_code": type(error).__name__})
    if not listings and failures:
        failure = failures[0]
        raise failure if isinstance(failure, DhilluSahiAuthError) else DhilluSahiAuthError("Every Dhillu & Sahi listing failed")
    return merge(listings)
