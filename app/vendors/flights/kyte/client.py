"""Kyte (gokyte.com) Flight API v3: low-cost and NDC airlines (easyJet, Ryanair, Wizz Air, Transavia, Jet2, IndiGo,
Volotea, AJet, ...). Docs: https://docs.sandbox.gokyte.com (API 3.1.28).

- Every call is POST JSON under {base}/api/v3/, with the API key in the `x-api-key` header (Integration Essentials).
- `x-request-id` is a new UUID per call; `x-transaction-id` stays the same for one shopping flow (search -> offer details
  -> booking) so Kyte support can follow it.
- Prices are integers in the currency's smallest unit, with the currency's `decimals` (Pricing, Costs and Currency).
- Kyte allows calls only from whitelisted IPs (the production server); the key comes from settings (SSM / .env), never code.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.config import settings

logger = logging.getLogger(__name__)

NAME = "kyte"
SHOP_PATH = "/api/v3/flights/shop"
OFFER_DETAILS_PATH = "/api/v3/flights/shop/offer-details"


class KyteError(RuntimeError):
    """A Kyte call failed. `public_message` is safe to show; the rest is not."""

    def __init__(self, error_code: str, public_message: str = "The flight supplier request failed.", *, status: int | None = None):
        super().__init__(error_code)
        self.error_code = error_code
        self.public_message = public_message
        self.status = status


class KyteNotConfigured(KyteError):
    def __init__(self):
        super().__init__("not_configured", "This flight supplier isn't connected.")


def configured() -> bool:
    key = settings.kyte_api_key
    return bool(settings.kyte_base_url and key and key.get_secret_value())


def new_transaction_id() -> str:
    return str(uuid.uuid4())


class KyteClient:
    def __init__(self, base_url: str | None = None, api_key: SecretStr | None = None, timeout_seconds: float | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        base_url = base_url if base_url is not None else settings.kyte_base_url
        api_key = api_key if api_key is not None else settings.kyte_api_key
        if not base_url or api_key is None or not api_key.get_secret_value():
            raise KyteNotConfigured()
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        seconds = timeout_seconds if timeout_seconds is not None else settings.kyte_timeout_seconds
        self.timeout = httpx.Timeout(seconds, connect=min(seconds, 5.0))
        self.transport = transport

    async def _post(self, operation: str, path: str, body: dict, *, transaction_id: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        headers = {"x-api-key": self._api_key.get_secret_value(), "content-type": "application/json", "accept": "application/json",
                   "x-request-id": str(uuid.uuid4()), "x-transaction-id": transaction_id}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(f"{self.base_url}{path}", json=body, headers=headers, params=params)
        except httpx.TimeoutException:
            logger.warning("Kyte call timed out", extra={"operation": operation})
            raise KyteError("timeout", "The flight supplier did not respond in time.") from None
        except httpx.HTTPError as error:
            logger.warning("Kyte call failed", extra={"operation": operation, "exception_type": type(error).__name__})
            raise KyteError("upstream_error", "The flight supplier can't be reached.") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code in (401, 403):
            logger.warning("Kyte refused the call", extra={"operation": operation, "status": response.status_code})
            raise KyteError("unauthorized", "The flight supplier refused the gateway's credentials.", status=response.status_code)
        if response.status_code >= 300 or not isinstance(data, dict):
            codes = [e.get("code") for e in (data or {}).get("errors", []) if isinstance(e, dict)] if isinstance(data, dict) else []
            logger.warning("Kyte %s returned an error", operation, extra={"status": response.status_code, "codes": codes[:5]})
            raise KyteError(f"{operation}_failed", f"The flight supplier couldn't complete the {operation}.", status=response.status_code)
        return data

    async def shop(self, body: dict, *, transaction_id: str, airlines: list[str] | None = None) -> dict[str, Any]:
        """Shop Flights. Always HTTP 200, even with nothing found: read `errors` (SEARCH_ERROR = no availability)."""
        params = [("airlines", code) for code in airlines or []] or None
        return await self._post("search", SHOP_PATH, body, transaction_id=transaction_id, params=params)

    async def offer_details(self, offer_ids: list[str], *, transaction_id: str) -> dict[str, Any]:
        """Offer Details for up to 5 offers: fare conditions, baggage, the current `amount` and `currency`, `expiration`."""
        return await self._post("offer details", OFFER_DETAILS_PATH, {"offerIds": offer_ids[:5]}, transaction_id=transaction_id)


def configured_client(transport: httpx.AsyncBaseTransport | None = None) -> KyteClient:
    return KyteClient(transport=transport)
