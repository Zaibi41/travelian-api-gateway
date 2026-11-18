"""SooperFare partner API (airline aggregator) client.

Contract: SooperFare's Postman collection ("SooperFare Shareable", also at documenter.getpostman.com/view/33772435/2sBXwjut1B)
for the request formats, and responses read live from the gateway's static IP on 18 November 2026 (tests/sooperfare/fixtures).
- Login: POST /v1/partner/auth/login {username, password} -> data.access_token.{token, expires_at}.
- Every other call sends only X-SF-Authorization: <token> (verified live: no Basic header or X-SF-Token needed).
- Availability: GET /v1/airline/availability with a JSON body (the airlines, "providers", that can be searched).
- Search: POST /v1/airline/search, once per provider (identifier from availability).
- Quote: POST /v1/airline/quote {ref_id, legs: [{ref_id, flight: {ref_id, fare: {ref_id}}}]} (one entry per flight).
- Reservation, booking confirm / cancel / void change supplier state: never retried (a timeout may still have booked).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

NAME = "sooperfare"


class SooperfareError(RuntimeError):
    def __init__(self, error_code: str, public_message: str = "The flight supplier request failed.", *, status: int | None = None):
        super().__init__(error_code)
        self.error_code = error_code
        self.public_message = public_message
        self.status = status


class SooperfareNotConfigured(SooperfareError):
    def __init__(self):
        super().__init__("not_configured", "This flight supplier isn't configured.")


def configured() -> bool:
    return bool(settings.sooperfare_base_url and settings.sooperfare_username and settings.sooperfare_password)


@dataclass
class _Session:
    token: str
    expires_at: float

    def fresh(self, now: float) -> bool:
        return now < self.expires_at - 60


_session: _Session | None = None


def _expiry(value: Any, now: float) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return now + 15 * 60  # unknown expiry: renew after 15 minutes


TRAVELER_TYPES = (("adult", "adults"), ("child", "children"), ("infant", "infants"))


def search_body(*, origin: str, destination: str, departure: date, return_date: date | None, adults: int, children: int,
                infants: int, cabin: str, provider: str | None = None) -> dict[str, Any]:
    legs = [{"from": origin, "to": destination, "date": departure.isoformat(), "return": None}]
    if return_date:
        legs.append({"from": destination, "to": origin, "date": return_date.isoformat(), "return": None})
    counts = {"adults": adults, "children": children, "infants": infants}
    body: dict[str, Any] = {
        "flexible_dates": False,
        "trip_type": "return" if return_date else "one_way",
        "cabin_class": cabin,
        "legs": legs,
        "travelers": [{"type": kind, "qty": counts[key]} for kind, key in TRAVELER_TYPES],
    }
    if provider:
        body.update({"redirect": True, "provider": provider})
    return body


class SooperfareClient:
    def __init__(self, http: httpx.AsyncClient | None = None):
        if not configured():
            raise SooperfareNotConfigured()
        self._http = http
        self._base = settings.sooperfare_base_url.rstrip("/")  # type: ignore[union-attr]

    async def _client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http is not None:
            return self._http, False
        return httpx.AsyncClient(timeout=settings.sooperfare_timeout_seconds), True

    async def _token(self) -> str:
        global _session
        now = time.time()
        if _session and _session.fresh(now):
            return _session.token
        data = await self._send("POST", "/v1/partner/auth/login",
                                {"username": settings.sooperfare_username,
                                 "password": settings.sooperfare_password.get_secret_value()},  # type: ignore[union-attr]
                                auth=False)
        access = (data.get("data") or {}).get("access_token") if isinstance(data.get("data"), dict) else None
        token = access.get("token") if isinstance(access, dict) else None
        if not isinstance(token, str) or not token:
            raise SooperfareError("login_failed", "The flight supplier refused the login.")
        _session = _Session(token, _expiry(access.get("expires_at"), now))
        return token

    async def _send(self, method: str, path: str, body: dict[str, Any], *, auth: bool = True, retry_login: bool = True) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            headers["X-SF-Authorization"] = await self._token()
        client, own = await self._client()
        try:
            # Availability is a GET with a JSON body, as SooperFare documents it.
            response = await client.request(method, self._base + path, json=body, headers=headers)
        except httpx.TimeoutException:
            raise SooperfareError("timeout", "The flight supplier took too long to answer.") from None
        except httpx.HTTPError as error:
            logger.warning("SooperFare request failed", extra={"path": path, "error_type": type(error).__name__})
            raise SooperfareError("unreachable") from None
        finally:
            if own:
                await client.aclose()
        if response.status_code == 401 and auth and retry_login:
            global _session
            _session = None
            return await self._send(method, path, body, auth=auth, retry_login=False)
        try:
            data = response.json()
        except ValueError:
            raise SooperfareError("invalid_response", status=response.status_code) from None
        if response.status_code >= 400 or not isinstance(data, dict) or data.get("status") is False:
            message = str(data.get("message") or "") if isinstance(data, dict) else ""
            logger.warning("SooperFare refused", extra={"path": path, "status": response.status_code, "supplier_message": message[:200]})
            raise SooperfareError("refused", message or "The flight supplier refused the request.", status=response.status_code)
        return data

    # ---------- read-only ----------

    async def availability(self, **search: Any) -> list[str]:
        data = await self._send("GET", "/v1/airline/availability", {**search_body(**search), "redirect": True})
        providers = ((data.get("data") or {}).get("providers")) or []
        return [str(p["identifier"]) for p in providers if isinstance(p, dict) and p.get("identifier")]

    async def search(self, *, provider: str, **search: Any) -> dict[str, Any]:
        return await self._send("POST", "/v1/airline/search", search_body(provider=provider, **search))

    async def quote(self, search_ref_id: str, selections: list[tuple[str, str, str]]) -> dict[str, Any]:
        """selections: (leg ref_id, flight ref_id, fare ref_id), one per flight (outbound, then return)."""
        legs = [{"ref_id": leg, "flight": {"ref_id": flight, "fare": {"ref_id": fare}}} for leg, flight, fare in selections]
        return await self._send("POST", "/v1/airline/quote", {"ref_id": search_ref_id, "legs": legs})

    # ---------- change supplier state: never retried ----------

    async def reservation(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._send("POST", "/v1/airline/reservation", body, retry_login=False)

    async def booking_list(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._send("POST", "/v1/booking/list", filters or {})

    async def confirm(self, booking_ref_id: str, txn_ref_id: str, amount: int, currency: str, remarks: str) -> dict[str, Any]:
        return await self._send("POST", "/v1/booking/confirm", {"booking_ref_id": booking_ref_id, "txn_ref_id": txn_ref_id,
                                                                "amount": amount, "currency": currency, "remarks": remarks}, retry_login=False)
