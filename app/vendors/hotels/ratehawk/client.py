"""RateHawk (Emerging Travel Group) HTTP client: B2B API v3 and Content API v1.

- HTTP Basic authentication: key id as user name, API key as password. No login call.
- JSON answers are {"data": ..., "status": "ok" | "error", "error": ...}; anything else is an error.
- GET operations that take input send it as JSON in `?data=`.
- Credentials are never logged or returned. Errors are logged by operation, code and HTTP status only.
- Calls that change supplier state are never retried (a timeout may still have booked or cancelled).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.config import settings
from app.vendors.hotels.ratehawk.operations import BY_KEY, Operation

logger = logging.getLogger(__name__)

NAME = "ratehawk"


class RateHawkError(RuntimeError):
    """Supplier call failed. `public_message` is safe to show; `error_code` is RateHawk's own code or ours."""

    def __init__(self, error_code: str, public_message: str = "The hotel supplier request failed.", *,
                 status: int | None = None, debug: Any = None):
        super().__init__(error_code)
        self.error_code = error_code
        self.public_message = public_message
        self.status = status
        #: RateHawk's `debug` block (request validation detail). Holds no credentials.
        self.debug = debug


class RateHawkNotConfigured(RateHawkError):
    def __init__(self):
        super().__init__("not_configured", "The hotel supplier is not configured.")


class RateHawkWritesDisabled(RateHawkError):
    def __init__(self):
        super().__init__("writes_disabled", "Calls that change bookings at the hotel supplier are turned off.")


@dataclass(frozen=True)
class Download:
    content: bytes
    content_type: str
    filename: str | None


class RateHawkClient:
    def __init__(self, base_url: str | None, key_id: str | None, api_key: SecretStr | None, *,
                 timeout_seconds: float = 30.0, writes_enabled: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not base_url or not key_id or api_key is None or not api_key.get_secret_value():
            raise RateHawkNotConfigured()
        self.base_url = base_url.rstrip("/")
        self._auth = httpx.BasicAuth(key_id, api_key.get_secret_value())
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
        self.writes_enabled = writes_enabled
        self.transport = transport

    # ---------- transport ----------

    async def _send(self, operation: Operation, payload: dict | None) -> httpx.Response:
        if operation.writes and not self.writes_enabled:
            raise RateHawkWritesDisabled()
        url = f"{self.base_url}{operation.path}"
        kwargs: dict[str, Any] = {"headers": {"Accept": "application/json"}}
        if operation.send == "json":
            kwargs["json"] = payload or {}
        elif operation.send == "query":
            kwargs["params"] = {"data": json.dumps(payload or {}, separators=(",", ":"))}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport, auth=self._auth) as client:
                return await client.request(operation.method, url, **kwargs)
        except httpx.TimeoutException:
            logger.warning("RateHawk call timed out", extra={"operation": operation.key})
            raise RateHawkError("timeout", "The hotel supplier did not respond in time.") from None
        except httpx.HTTPError as error:
            logger.warning("RateHawk call failed", extra={"operation": operation.key, "exception_type": type(error).__name__})
            raise RateHawkError("upstream_error", "The hotel supplier can't be reached.") from None

    @staticmethod
    def _http_error(operation: Operation, status: int, body: Any) -> RateHawkError:
        code = body.get("error") if isinstance(body, dict) and body.get("error") else f"http_{status}"
        logger.warning("RateHawk answered with an error", extra={"operation": operation.key, "supplier_status": status, "error_code": code})
        if status in (401, 403):
            return RateHawkError(str(code), "The hotel supplier refused the gateway's credentials.", status=status)
        if status == 429:
            return RateHawkError("rate_limited", "The hotel supplier's request limit was reached. Try again in a minute.", status=status)
        debug = body.get("debug") if isinstance(body, dict) else None
        return RateHawkError(str(code), "The hotel supplier could not complete the request.", status=status, debug=debug)

    async def call(self, key: str, payload: dict | None = None) -> Any:
        """Run one JSON operation and return its `data`."""
        operation = BY_KEY[key]
        if operation.download:
            raise ValueError(f"{key} returns a file; use download()")
        response = await self._send(operation, payload)
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400 or not isinstance(body, dict) or body.get("status") != "ok":
            if response.status_code < 400 and not isinstance(body, dict):
                logger.warning("RateHawk answer was not JSON", extra={"operation": key, "supplier_status": response.status_code})
                raise RateHawkError("invalid_response", "The hotel supplier returned an unusable response.", status=response.status_code)
            raise self._http_error(operation, response.status_code, body)
        return body.get("data")

    async def download(self, key: str, payload: dict) -> Download:
        """Run one file operation (voucher, invoices, closing documents)."""
        operation = BY_KEY[key]
        if not operation.download:
            raise ValueError(f"{key} returns JSON; use call()")
        response = await self._send(operation, payload)
        content_type = response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        if response.status_code >= 400 or content_type == "application/json":
            try:
                body = response.json()
            except ValueError:
                body = None
            raise self._http_error(operation, response.status_code if response.status_code >= 400 else 422, body)
        filename = None
        disposition = response.headers.get("content-disposition", "")
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('"') or None
        return Download(response.content, content_type, filename)

    # ---------- the calls hotel search uses ----------

    async def suggest(self, query: str, language: str) -> dict:
        return await self.call("multicomplete", {"query": query, "language": language}) or {}

    async def search_region(self, *, region_id: int, **common) -> dict:
        return await self.call("search_region", {**common, "region_id": region_id}) or {}

    async def search_hotels(self, *, hids: list[int], **common) -> dict:
        return await self.call("search_hotels", {**common, "hids": hids}) or {}

    async def content_by_ids(self, hids: list[int], language: str) -> list[dict]:
        data = await self.call("content_by_ids", {"hids": hids, "language": language})
        return data if isinstance(data, list) else []


def configured_client() -> RateHawkClient:
    return RateHawkClient(
        settings.ratehawk_endpoint, settings.ratehawk_key_id, settings.ratehawk_api_key,
        timeout_seconds=settings.ratehawk_timeout_seconds, writes_enabled=settings.ratehawk_write_enabled,
    )
