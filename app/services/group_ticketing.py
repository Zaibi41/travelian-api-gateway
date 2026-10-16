import logging
from collections.abc import Awaitable, Callable
from datetime import date

from app.schemas.group_ticketing import GroupInventory
from app.services.vendor_fanout import gather_vendor_calls
from app.vendors.group_ticketing.dhillu_sahi.inventory import fetch_inventory as fetch_dhillu_sahi_inventory
from app.vendors.group_ticketing.dhillu_sahi.inventory import is_configured as dhillu_sahi_is_configured
from app.vendors.group_ticketing.skypass.inventory import fetch_inventory as fetch_skypass_inventory
from app.vendors.group_ticketing.skypass.inventory import is_configured as skypass_is_configured

logger = logging.getLogger(__name__)


class GroupInventoryUnavailable(RuntimeError):
    """No configured group inventory supplier returned a usable response."""


def get_group_inventory_vendors() -> list[tuple[str, Callable[[], Awaitable[list[GroupInventory]]]]]:
    """Registry of configured group inventory suppliers; add vendors only with their API contracts."""
    vendors: list[tuple[str, Callable[[], Awaitable[list[GroupInventory]]]]] = []
    if dhillu_sahi_is_configured():
        vendors.append(("dhillu_sahi", fetch_dhillu_sahi_inventory))
    if skypass_is_configured():
        vendors.append(("skypass", fetch_skypass_inventory))
    return vendors


async def fetch_all_group_inventory() -> list[GroupInventory]:
    vendors = get_group_inventory_vendors()
    if not vendors:
        raise GroupInventoryUnavailable("Group inventory is not configured")

    results, issues, _ = await gather_vendor_calls(vendors, allow_empty_result=True)
    for vendor, groups in results.items():
        logger.info(
            "Group inventory supplier returned records",
            extra={"vendor": vendor, "group_count": len(groups)},
        )
    for issue in issues:
        logger.warning(
            "Group inventory supplier unavailable",
            extra={"vendor": issue.vendor, "error_code": issue.code},
        )

    if not results:
        raise GroupInventoryUnavailable("Group inventory is temporarily unavailable")

    return [group for vendor_groups in results.values() for group in vendor_groups]


def filter_group_inventory(
    groups: list[GroupInventory],
    *,
    origin: str,
    destination: str,
    departure_date: date,
    adults: int,
) -> list[GroupInventory]:
    """Filter normalized one-way group inventory by sector, date, and known remaining seats."""
    origin = origin.strip().upper()
    destination = destination.strip().upper()
    filtered: list[GroupInventory] = []

    for group in groups:
        matching_tickets = []
        for ticket in group.tickets:
            route = [part.strip().upper() for part in (ticket.sector or "").split("-")]
            if len(route) < 2 or route[0] != origin or route[-1] != destination:
                continue
            if ticket.departure_date != departure_date.isoformat():
                continue
            # Keep inventory with unknown capacity visible; filter out only known insufficient stock.
            if ticket.remaining is not None and ticket.remaining < adults:
                continue
            matching_tickets.append(ticket)

        if matching_tickets:
            filtered.append(group.model_copy(update={"tickets": matching_tickets}))
    return filtered
