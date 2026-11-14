from typing import Any

import httpx


class DhilluSahiAuthError(RuntimeError):
    """Raised when Dhillu & Sahi authentication fails or returns an invalid body."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "upstream_error",
        public_message: str = "Supplier request failed.",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.public_message = public_message


class DhilluSahiAuthClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 3.0))
        self.transport = transport
        self.agent_id: int | None = None

    def _new_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

    async def login(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate using the documented JSON body and return the success payload."""
        async with self._new_http_client() as client:
            response = await self._post_json(
                client,
                "/api/login",
                json={"email": email, "password": password},
                operation="login",
            )
        try:
            success = response["success"]
            token = success["token"]
        except (TypeError, KeyError) as exc:
            raise DhilluSahiAuthError("Dhillu & Sahi login response did not match the expected schema") from exc
        if not isinstance(token, str) or not token.strip():
            raise DhilluSahiAuthError("Dhillu & Sahi login response contained an invalid token")
        agent_id = success.get("agent_id")
        self.agent_id = agent_id if type(agent_id) is int and agent_id > 0 else None
        return success

    async def fetch_groups(self, token: str, sector_id: str | None = None) -> list[dict[str, Any]]:
        """Fetch group inventory for the authenticated user."""
        body = {"my_sector_id": sector_id} if sector_id is not None else {}
        async with self._new_http_client() as client:
            payload = await self._post_json(
                client,
                "/api/pnr-respect-to-mysector-user",
                json=body,
                operation="group inventory",
                token=token,
            )
        if payload.get("success") is not True or not isinstance(payload.get("data"), list):
            raise DhilluSahiAuthError("Dhillu & Sahi group response did not match the expected schema")
        return payload["data"]

    async def check_availability(
        self,
        token: str,
        *,
        pnr: str,
        adult: int,
        child: int,
        price: int | float,
    ) -> bool:
        """Check seat availability for a PNR using the agent-side endpoint."""
        async with self._new_http_client() as client:
            payload = await self._post_json(
                client,
                "/api/check-availbility",
                json={"pnr": pnr, "adult": adult, "child": child, "price": price},
                operation="availability check",
                token=token,
            )
        data = payload.get("data")
        if payload.get("success") is not True or not isinstance(data, dict) or not isinstance(data.get("status"), bool):
            raise DhilluSahiAuthError("Dhillu & Sahi availability response did not match the expected schema")
        return data["status"]

    async def add_booking(self, token: str, booking: dict[str, Any]) -> int:
        """Create a supplier booking. This state-changing operation is never retried."""
        async with self._new_http_client() as client:
            payload = await self._post_json(
                client,
                "/api/add-booking",
                json=booking,
                operation="booking creation",
                token=token,
            )
        data = payload.get("data")
        booking_id = data.get("id") if isinstance(data, dict) else None
        if payload.get("success") is not True or (type(booking_id) is not int or booking_id <= 0):
            raise DhilluSahiAuthError("Dhillu & Sahi booking response did not match the expected schema")
        return booking_id

    async def create_booking(self, token: str, booking: dict[str, Any]) -> dict[str, Any]:
        """add-booking, returning the supplier's whole answer. Never retried. success false: error_code
        'rejected' (nothing booked); success true without a usable booking id: 'invalid_response' (unclear)."""
        async with self._new_http_client() as client:
            payload = await self._post_json(client, "/api/add-booking", json=booking, operation="booking creation", token=token)
        if payload.get("success") is not True:
            raise DhilluSahiAuthError("Dhillu & Sahi refused the booking", error_code="rejected")
        data = payload.get("data")
        booking_id = data.get("id") if isinstance(data, dict) else None
        if type(booking_id) is not int or booking_id <= 0:
            raise DhilluSahiAuthError("Dhillu & Sahi booking response had no booking id", error_code="invalid_response")
        return payload

    async def fetch_booking_details(self, token: str, booking_id: int) -> dict[str, Any]:
        """Fetch and validate booking and associated group details."""
        async with self._new_http_client() as client:
            payload = await self._post_json(
                client,
                "/api/agent-booking-detail",
                json={"booking_id": booking_id},
                operation="booking details",
                token=token,
            )
        data = payload.get("data")
        if (
            payload.get("success") is not True
            or not isinstance(data, dict)
            or not isinstance(data.get("data"), dict)
            or not isinstance(data.get("groupdata"), dict)
        ):
            raise DhilluSahiAuthError("Dhillu & Sahi booking details did not match the expected schema")
        return {
            "booking": data["data"],
            "group": data["groupdata"],
            "message": payload.get("message"),
        }

    async def _post_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        json: dict[str, Any],
        operation: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await client.post(f"{self.base_url}{path}", headers=headers, json=json)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise DhilluSahiAuthError(
                f"Dhillu & Sahi {operation} timed out",
                error_code="upstream_timeout",
                public_message="Supplier did not respond before the timeout.",
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            raise DhilluSahiAuthError(
                f"Dhillu & Sahi {operation} returned HTTP {status_code}",
                error_code=f"upstream_http_{status_code}",
                public_message=f"Supplier returned HTTP {status_code}.",
            ) from exc
        except httpx.HTTPError as exc:
            raise DhilluSahiAuthError(f"Dhillu & Sahi {operation} request failed") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise DhilluSahiAuthError(f"Dhillu & Sahi {operation} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DhilluSahiAuthError(f"Dhillu & Sahi {operation} response must be a JSON object")
        return payload
