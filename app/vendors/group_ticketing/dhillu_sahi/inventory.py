from app.core.config import settings
from app.vendors.group_ticketing.dhillu_sahi.auth import DhilluSahiAuthClient
from app.vendors.group_ticketing.dhillu_sahi.groups import GroupInventory, normalize_groups


def is_configured() -> bool:
    return bool(
        settings.dhillu_sahi_email
        and settings.dhillu_sahi_password
        and settings.dhillu_sahi_group_sector_id
    )


async def fetch_inventory() -> list[GroupInventory]:
    """Fetch all group inventory using the supplier's documented no-filter request."""
    if not is_configured():
        raise RuntimeError("Dhillu & Sahi credentials are not configured")

    client = DhilluSahiAuthClient(
        settings.dhillu_sahi_base_url,
        timeout_seconds=settings.dhillu_sahi_timeout_seconds,
    )
    password = settings.dhillu_sahi_password
    login = await client.login(
        settings.dhillu_sahi_email or "",
        password.get_secret_value() if password else "",
    )
    rows = await client.fetch_groups(login["token"], settings.dhillu_sahi_group_sector_id)
    return normalize_groups(rows)
