"""Supplier-hosted logos served through the gateway (D23: no supplier host reaches a client).

A logo URL from supplier inventory is registered under an opaque ID; clients load
`/v1/logos/{id}`. Only registered IDs are served. Fetching is https-only, to public
addresses, without redirects, images only (no SVG), size- and time-capped, and cached.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
import socket
import time
from collections import OrderedDict
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_BYTES = 256 * 1024
TTL_SECONDS = 24 * 3600
MAX_ENTRIES = 1000
TIMEOUT_SECONDS = 5.0
#: Per-process key so IDs can't be derived from a known supplier URL.
_KEY = os.urandom(32)

_urls: OrderedDict[str, str] = OrderedDict()
#: The airline each logo belongs to, only for the log line when it can't be served ("Airblue (PA)").
_airlines: dict[str, str] = {}
_images: OrderedDict[str, tuple[float, str, bytes]] = OrderedDict()


def register(url: str | None, airline: str | None = None) -> str | None:
    """Opaque path for a supplier logo URL, or None when it can't be served. `airline`: name and code for the log."""
    if not url or urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
        return None
    logo_id = hmac.new(_KEY, url.encode(), hashlib.sha256).hexdigest()[:32]
    _urls[logo_id] = url
    _urls.move_to_end(logo_id)
    if airline:
        _airlines[logo_id] = airline
    while len(_urls) > MAX_ENTRIES:
        dropped, _ = _urls.popitem(last=False)
        _airlines.pop(dropped, None)
    return f"/v1/logos/{logo_id}"


def airline_label(name: str | None, flight_number: str | None = None) -> str | None:
    """'Airblue (PA)': the name and the airline code from the flight number, for logging only."""
    code = (flight_number or "").strip()[:2].upper()
    return f"{name} ({code})" if name and code.isalnum() and len(code) == 2 else name or (code or None)


def _missing(logo_id: str, reason: str) -> LogoUnavailable:
    # The page shows the airline name instead; this says which airline had no logo, and why.
    logger.warning("Airline logo not available", extra={"airline": _airlines.get(logo_id, "unknown"), "reason": reason})
    return LogoUnavailable()


class LogoUnavailable(Exception):
    pass


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    return bool(addresses) and all(address.is_global for address in addresses)


async def fetch(logo_id: str, client_factory=None) -> tuple[str, bytes]:
    cached = _images.get(logo_id)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        return cached[1], cached[2]
    url = _urls.get(logo_id)
    if url is None:
        # Unknown id: logo ids live in memory with a per-process key, so a gateway restart (e.g. --reload) forgets
        # the ids a page already has. The next search issues fresh ones.
        raise _missing(logo_id, "unknown_id")
    host = urlsplit(url).hostname or ""
    if not await asyncio.to_thread(_public_host, host):
        raise _missing(logo_id, "host_not_public")
    try:
        async with (client_factory or httpx.AsyncClient)(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if response.status_code != 200 or content_type not in ALLOWED_TYPES:
                    raise _missing(logo_id, f"supplier_answer_{response.status_code}" if response.status_code != 200 else "not_an_image")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise _missing(logo_id, "too_large")
    except httpx.HTTPError:
        raise _missing(logo_id, "fetch_failed") from None
    _images[logo_id] = (time.monotonic(), content_type, bytes(body))
    while len(_images) > MAX_ENTRIES:
        _images.popitem(last=False)
    return content_type, bytes(body)
