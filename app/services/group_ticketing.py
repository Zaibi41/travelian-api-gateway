import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from uuid import NAMESPACE_URL, uuid5

from app.schemas.group_ticketing import GroupInventory, GroupTicket
from app.services.vendor_fanout import gather_vendor_calls
from app.vendors.group_ticketing.al_haider.inventory import fetch_inventory as fetch_al_haider_inventory
from app.vendors.group_ticketing.al_haider.inventory import is_configured as al_haider_is_configured
from app.vendors.group_ticketing.dhillu_sahi.inventory import fetch_inventory as fetch_dhillu_sahi_inventory
from app.vendors.group_ticketing.dhillu_sahi.inventory import is_configured as dhillu_sahi_is_configured
from app.vendors.group_ticketing.skypass.inventory import fetch_inventory as fetch_skypass_inventory
from app.vendors.group_ticketing.skypass.inventory import is_configured as skypass_is_configured

logger = logging.getLogger(__name__)


class GroupInventoryUnavailable(RuntimeError):
    """No configured group inventory supplier returned a usable response."""


#: Group stock Travelian owns would come from this source; every configured vendor today is a supplier.
OWNED_SOURCE = "travelian"


def sector_codes(sector: str | None) -> tuple[str, str] | None:
    """'LHE - DMM' -> ('LHE', 'DMM'): first and last airport of a sector."""
    parts = [part.strip().upper() for part in (sector or "").split("-") if part.strip()]
    return (parts[0], parts[-1]) if len(parts) >= 2 and all(len(part) == 3 and part.isalpha() for part in (parts[0], parts[-1])) else None


def stock_label(vendor: str) -> str:
    """'owned' for Travelian's own group stock, 'supplier' otherwise. Safe for every viewer: it never names the supplier."""
    return "owned" if vendor == OWNED_SOURCE else "supplier"


class TicketNotFound(LookupError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def find_ticket(ticket_id: str, fetch: Callable[[], Awaitable[list[GroupInventory]]] | None = None) -> GroupTicket:
    """One ticket from refreshed combined inventory by its opaque ID (no supplier detail endpoint is
    called). 503 inventory down, 404 gone, 409 ambiguous."""
    try:
        groups = await (fetch or fetch_all_group_inventory)()
    except GroupInventoryUnavailable as exc:
        raise TicketNotFound(503, "Group inventory is temporarily unavailable") from exc
    matches = [ticket for group in groups for ticket in group.tickets if group_ticket_id(ticket) == ticket_id]
    if not matches:
        raise TicketNotFound(404, "Ticket is not available in current inventory")
    if len(matches) != 1:
        raise TicketNotFound(409, "Ticket selection is ambiguous; refresh inventory")
    return matches[0]


def get_group_inventory_vendors() -> list[tuple[str, Callable[[], Awaitable[list[GroupInventory]]]]]:
    """Registry of configured group inventory suppliers; add vendors only with their API contracts."""
    vendors: list[tuple[str, Callable[[], Awaitable[list[GroupInventory]]]]] = []
    if dhillu_sahi_is_configured():
        vendors.append(("dhillu_sahi", fetch_dhillu_sahi_inventory))
    if skypass_is_configured():
        vendors.append(("skypass", fetch_skypass_inventory))
    if al_haider_is_configured():
        vendors.append(("al_haider", fetch_al_haider_inventory))
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
    origin: str | None = None,
    destination: str | None = None,
    departure_date: date | None = None,
    adults: int = 1,
) -> list[GroupInventory]:
    """Filter normalized group inventory (one-way and return) by any of: outbound origin, outbound destination,
    departure date; and by known remaining seats. Empty filters match everything. Tickets and groups come back in
    departure date order (undated last)."""
    origin = (origin or "").strip().upper() or None
    destination = (destination or "").strip().upper() or None
    filtered: list[GroupInventory] = []

    for group in groups:
        matching_tickets = []
        for ticket in group.tickets:
            codes = sector_codes(ticket.sector)
            if (origin or destination) and codes is None:
                continue
            if origin and codes[0] != origin:
                continue
            if destination and codes[1] != destination:
                continue
            if departure_date and ticket.departure_date != departure_date.isoformat():
                continue
            # Keep inventory with unknown capacity visible; filter out only known insufficient stock.
            if ticket.remaining is not None and ticket.remaining < adults:
                continue
            matching_tickets.append(ticket)

        if matching_tickets:
            matching_tickets.sort(key=_date_key)
            filtered.append(group.model_copy(update={"tickets": matching_tickets}))
    filtered.sort(key=lambda group: _date_key(group.tickets[0]))
    return filtered


def _date_key(ticket: GroupTicket) -> tuple[bool, str]:
    return (not ticket.departure_date, ticket.departure_date or "")


def group_ticket_id(ticket: GroupTicket) -> str:
    """Identify the supplier record without exposing supplier routing in the ID.

    Exclude mutable price, capacity and timestamp so a refresh can return updated
    values. No source ordering or synthetic PNR is used. Ambiguous matches must
    be rejected by selection rather than choosing an arbitrary record.
    """
    identity = {
        key: getattr(ticket, key)
        for key in ("vendor", "pnr", "departure_date", "sector", "ticket_type", "subparts")
    }
    return str(uuid5(NAMESPACE_URL, json.dumps(identity, sort_keys=True)))
