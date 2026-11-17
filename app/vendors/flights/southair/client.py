"""Southair (AEROST OTA API) HTTP client.

- Every call is POST, form-encoded, under {base}/api/ota/v1/.
- Login issues a bearer token valid for 7 days. It is cached and reused; login runs
  again only when the supplier answers 401 / "unauthenticated." (guide 2.1).
- Dates are sent as DD-MM-YYYY.
- Credentials, tokens, NLD, fare hashes and ticket_ref are never logged or returned
  to clients. Errors are logged by type and supplier status only.
- Ticketing is never retried, not even after a 401.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

logger = logging.getLogger(__name__)

NAME = "southair"
TOKEN_LIFETIME_SECONDS = 7 * 24 * 3600
TOKEN_REFRESH_MARGIN_SECONDS = 3600


class SouthairError(RuntimeError):
    """Supplier call failed. `public_message` is safe to show; the rest is not."""

    def __init__(self, error_code: str, public_message: str = "Supplier request failed.", *, status: int | None = None):
        super().__init__(error_code)
        self.error_code = error_code
        self.public_message = public_message
        self.status = status


class SouthairNotConfigured(SouthairError):
    def __init__(self):
        super().__init__("not_configured", "The flight supplier is not configured.")


def supplier_date(value: date) -> str:
    return value.strftime("%d-%m-%Y")


@dataclass
class _Token:
    value: str
    issued_at: float

    def fresh(self, now: float) -> bool:
        return now - self.issued_at < TOKEN_LIFETIME_SECONDS - TOKEN_REFRESH_MARGIN_SECONDS


class SouthairClient:
    # Shared across instances: one token per process (guide: avoid unnecessary logins).
    _token: _Token | None = None
    _lock: asyncio.Lock | None = None

    def __init__(self, base_url: str | None, username: str | None, password: SecretStr | None,
                 timeout_seconds: float = 20.0, transport: httpx.AsyncBaseTransport | None = None, clock=time.time):
        if not base_url or not username or password is None or not password.get_secret_value():
            raise SouthairNotConfigured()
        self.base_url = base_url.rstrip("/") + "/api/ota/v1"
        self._username = username
        self._password = password
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
        self.transport = transport
        self.clock = clock

    @classmethod
    def reset_token(cls) -> None:
        cls._token = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    # ---------- transport ----------

    async def _raw_post(self, path: str, data: list[tuple[str, str]], token: str | None) -> tuple[int, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(f"{self.base_url}{path}", content=urlencode(data), headers=headers)
        except httpx.TimeoutException:
            logger.warning("Southair call timed out", extra={"operation": path.split("/")[-1]})
            raise SouthairError("timeout", "The flight supplier did not respond in time.") from None
        except httpx.HTTPError as error:
            logger.warning("Southair call failed", extra={"operation": path.split("/")[-1], "exception_type": type(error).__name__})
            raise SouthairError("upstream_error", "The flight supplier can't be reached.") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body

    @staticmethod
    def _unauthenticated(status: int, body: Any) -> bool:
        if status == 401:
            return True
        return isinstance(body, dict) and (body.get("status") == 401 or str(body.get("message", "")).strip().lower() == "unauthenticated.")

    async def _token_value(self, force: bool = False) -> str:
        async with self._get_lock():
            current = SouthairClient._token
            if current and not force and current.fresh(self.clock()):
                return current.value
            status, body = await self._raw_post(
                "/login", [("username", self._username), ("password", self._password.get_secret_value())], token=None,
            )
            token = (body or {}).get("data", {}).get("token") if isinstance(body, dict) else None
            if status >= 300 or not isinstance(token, str) or not token.strip():
                logger.warning("Southair login failed", extra={"status": status})
                raise SouthairError("login_failed", "The flight supplier rejected the gateway's credentials.", status=status)
            SouthairClient._token = _Token(token, self.clock())
            return token

    async def _call(self, operation: str, path: str, data: list[tuple[str, str]], *, retry_on_401: bool = True) -> dict[str, Any]:
        token = await self._token_value()
        status, body = await self._raw_post(path, data, token)
        if self._unauthenticated(status, body):
            # Token expired or revoked: log in again. Ticketing is never re-sent.
            token = await self._token_value(force=True)
            if not retry_on_401:
                raise SouthairError("unauthenticated", "The flight supplier session expired. Nothing was sent; try again.", status=401)
            status, body = await self._raw_post(path, data, token)
        if not isinstance(body, dict) or status >= 300 or int(body.get("status_code") or 0) != 200:
            supplier_status = body.get("status_code") if isinstance(body, dict) else None
            logger.warning("Southair %s returned an error", operation, extra={"status": status, "supplier_status": supplier_status})
            raise SouthairError(f"{operation}_failed", f"The flight supplier couldn't complete the {operation}.", status=status)
        data_out = body.get("data")
        return data_out if isinstance(data_out, dict) else {}

    # ---------- documented operations ----------

    async def profile(self) -> dict[str, Any]:
        return await self._call("profile", "/my-profile", [])

    async def search(self, *, origin: str, destination: str, departure: date, return_date: date | None,
                     adults: int, children: int, infants: int) -> dict[str, Any]:
        data = [
            ("adult", str(adults)), ("child", str(children)), ("infant", str(infants)),
            ("travel_type", "RT" if return_date else "OW"),
            ("dep_city[0]", origin), ("arr_city[0]", destination), ("dep_date[0]", supplier_date(departure)),
        ]
        if return_date:
            data.append(("ret_date[0]", supplier_date(return_date)))
        return await self._call("search", "/res/flights", data)

    async def fare_info(self, *, nld: str, selections: list[tuple[str, str]]) -> dict[str, Any]:
        """selections: [(route key e.g. "KHIMUX", fare hash), ...] in leg order."""
        data = [("carsf", nld)] + [(f"__flt[{index}][{route}]", fare_hash) for index, (route, fare_hash) in enumerate(selections)]
        return await self._call("fare_info", "/res/fare_info", data)

    async def booking(self, *, nld: str, acceptances: list[str], contact: dict[str, str],
                      passengers: list[dict[str, str]], remarks: str = "") -> dict[str, Any]:
        """passengers: dicts with type (ADT/CHD/INF) and the documented passenger fields."""
        data = [("nld", nld)] + [(f"acceptance[{index}]", value) for index, value in enumerate(acceptances)]
        data += [("email", contact["email"]), ("code1", contact["code"]), ("phone_no1", contact["phone"])]
        if remarks:
            data.append(("booking_remarks", remarks[:100]))
        counters = {"ADT": 0, "CHD": 0, "INF": 0}
        for passenger in passengers:
            kind = passenger["type"]
            index = counters[kind]
            counters[kind] += 1
            for key in ("first_name", "last_name", "date_of_birth", "name_title", "id_type", "passport_no",
                        "passport_issue", "passport_expiry", "origin_country", "infant_link_id"):
                value = passenger.get(key)
                if value not in (None, ""):
                    data.append((f"{key}[{kind}][{index}]", str(value)))
        return await self._call("booking", "/res/booking", data)

    async def ticketing(self, *, ticket_ref: str, cash_amount: str) -> dict[str, Any]:
        """Issue the ticket. Never retried (not even after a 401)."""
        return await self._call(
            "ticketing", "/res/ticketing",
            [("ticket_ref", ticket_ref), ("payment_method", "CASH"), ("cash_amount", cash_amount)],
            retry_on_401=False,
        )

    async def pnr_info(self, pnr: str) -> dict[str, Any]:
        if not pnr.isalnum() or len(pnr) > 16:
            raise SouthairError("invalid_pnr", "Invalid PNR.")
        return await self._call("pnr lookup", f"/res/pnr_info/{pnr}", [])


def configured_client(transport: httpx.AsyncBaseTransport | None = None) -> SouthairClient:
    from app.core.config import settings

    return SouthairClient(settings.southair_base_url, settings.southair_username, settings.southair_password,
                          settings.southair_timeout_seconds, transport=transport)
