"""Airlines never held automatically (D32, B6): IATA codes matched against the marketing and operating carriers.

Source: Parameter Store <SSM_PARAMETER_PREFIX>/flights/restricted-airlines (comma-separated IATA codes, e.g. "EY,QR"),
re-read at most every REFRESH_SECONDS so the list changes without a redeploy; RESTRICTED_AIRLINES when Parameter Store
isn't configured or can't be read (the last good list is kept on a read failure).
"""

from __future__ import annotations

import logging
import time

from app.core.config import settings

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 300
PARAMETER = "flights/restricted-airlines"
_cache: tuple[float, frozenset[str]] | None = None


def _parse(text: str) -> frozenset[str]:
    return frozenset(code.strip().upper() for code in text.replace(";", ",").split(",") if len(code.strip()) in (2, 3))


def _from_parameter_store() -> frozenset[str] | None:
    prefix = (settings.ssm_parameter_prefix or "").strip().strip("/")
    if not prefix:
        return None
    try:
        import boto3

        client = boto3.client("ssm", region_name=settings.aws_region) if settings.aws_region else boto3.client("ssm")
        value = client.get_parameter(Name=f"/{prefix}/{PARAMETER}")["Parameter"]["Value"]
        return _parse(value)
    except Exception as error:  # noqa: BLE001 - keep the last good list
        logger.warning("Restricted airlines not read from Parameter Store", extra={"error_type": type(error).__name__})
        return None


def codes(now: float | None = None) -> frozenset[str]:
    global _cache
    now = time.monotonic() if now is None else now
    if _cache and _cache[0] > now:
        return _cache[1]
    stored = _from_parameter_store()
    value = stored if stored is not None else (_cache[1] if _cache and settings.ssm_parameter_prefix else _parse(settings.restricted_airlines))
    _cache = (now + REFRESH_SECONDS, value)
    return value


def is_restricted(*carrier_codes: str | None) -> bool:
    restricted = codes()
    return bool(restricted) and any((code or "").upper() in restricted for code in carrier_codes if code)


def segment_restricted(segment: dict | None) -> bool:
    if not segment:
        return False
    return is_restricted(segment.get("airline_code"), *(segment.get("operating_codes") or []))
