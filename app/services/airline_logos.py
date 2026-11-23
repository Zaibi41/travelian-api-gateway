"""Airline logos from the website's one shared manifest (gateway#62, travelian#111).

The website, the admin and this gateway's itinerary / e-ticket PDF all use public/airline-logos/index.json, served at
`{AIRLINE_LOGOS_SITE_URL}/airline-logos/index.json` (default: the public site). The gateway keeps no copies: adding or
changing a logo in the manifest changes the PDF too. Files are read on demand and kept in memory for a while.

Safety: only the site's own `/airline-logos/*.png|jpg|jpeg` paths are fetched (no other host, no redirects), only PNG or
JPEG images up to 2 MB. Anything missing, slow or wrong gives None and the PDF prints the IATA code badge instead.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger(__name__)

MANIFEST_PATH = "/airline-logos/index.json"
LOGO_PATH = re.compile(r"^/airline-logos/[a-z0-9][a-z0-9-]*\.(png|jpe?g)$")
IMAGE_TYPES = {"image/png", "image/jpeg"}
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 4.0
TTL_SECONDS = 3600
FAILED_TTL_SECONDS = 300  # a failed read is retried after this long, not on every PDF

_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


@dataclass(frozen=True)
class Manifest:
    codes: dict[str, str]
    logos: dict[str, str]


def _site() -> str:
    return (getattr(settings, "airline_logos_site_url", None) or settings.public_site_url).rstrip("/")


def _get(url: str):
    import httpx

    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        return client.get(url)


def _cached(key: str, load):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = load()
    with _lock:
        _cache[key] = (now + (TTL_SECONDS if value is not None else FAILED_TTL_SECONDS), value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def manifest() -> Manifest | None:
    def load() -> Manifest | None:
        try:
            answer = _get(_site() + MANIFEST_PATH)
            data = answer.json() if answer.status_code == 200 else None
            codes, logos = (data or {}).get("codes"), (data or {}).get("logos")
            if not isinstance(codes, dict) or not isinstance(logos, dict):
                raise ValueError("not a logo manifest")
            return Manifest(codes={str(k).lower(): str(v).upper() for k, v in codes.items()},
                            logos={str(k).lower(): str(v) for k, v in logos.items()})
        except Exception as error:  # noqa: BLE001 - the PDF falls back to code badges
            logger.warning("Airline logo manifest not read", extra={"error_type": type(error).__name__})
            return None
    return _cached("manifest", load)


def resolve(code: str | None, name: str | None) -> tuple[str | None, str | None]:
    """(IATA code, logo path in the manifest) for an airline code and/or name; either may be None."""
    found = manifest()
    code = (code or "").strip().upper() or None
    key = (name or "").strip().lower()
    if found is None:
        return code, None
    code = code or found.codes.get(key)
    path = (found.logos.get(code.lower()) if code else None) or found.logos.get(key)
    return code, path


def image_bytes(path: str | None) -> bytes | None:
    """The logo file's bytes (PNG / JPEG, at most 2 MB), or None."""
    if not path or not LOGO_PATH.match(path):
        return None

    def load() -> bytes | None:
        try:
            answer = _get(_site() + path)
            kind = (answer.headers.get("content-type") or "").split(";")[0].strip().lower()
            if answer.status_code != 200 or kind not in IMAGE_TYPES or not answer.content or len(answer.content) > MAX_BYTES:
                return None
            return answer.content
        except Exception as error:  # noqa: BLE001
            logger.warning("Airline logo not read", extra={"logo": path, "error_type": type(error).__name__})
            return None
    return _cached(f"file:{path}", load)


def logo_for(code: str | None, name: str | None) -> tuple[str | None, bytes | None]:
    """(IATA code, logo bytes or None) for the PDF."""
    code, path = resolve(code, name)
    return code, image_bytes(path)
