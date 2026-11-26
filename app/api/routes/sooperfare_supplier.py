"""Raw SooperFare console: every SooperFare partner call, unchanged, Super Admin only.

Paths mirror SooperFare's own: SooperFare's `POST /v1/airline/search` is
`POST /v1/admin/suppliers/sooperfare/raw/v1/airline/search` here, with the same JSON body and query string. The answer
is SooperFare's body unchanged, with SooperFare's HTTP status (401/403 from SooperFare come back as 502, so they aren't
mistaken for a Travelian sign-in problem). `X-Supplier-Status` always carries SooperFare's own status.

Why a gateway route: SooperFare only accepts calls from the gateway's whitelisted IP, and the gateway holds the login.
The SooperFare token is never returned. Raw data (supplier cost, airline IDs) is why this is Super Admin only (D23).

Calls that change a booking (reservation, submit extras, confirm = issue and charge, cancel, void) answer 409 unless
SOOPERFARE_RAW_WRITE_ENABLED is on, and are never retried: a timeout there is reported as "outcome unknown".
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.field_visibility import BackOfficeUser, require_super_admin
from app.vendors.flights.sooperfare.client import SooperfareClient, SooperfareError, SooperfareNotConfigured
from app.vendors.flights.sooperfare.operations import OPERATIONS, Operation

logger = logging.getLogger(__name__)

PREFIX = "/admin/suppliers/sooperfare"
router = APIRouter(
    prefix=PREFIX,
    tags=["sooperfare supplier"],
    responses={401: {"description": "Not signed in."}, 403: {"description": "Super Admin only."}},
)
SuperAdmin = Annotated[BackOfficeUser, Depends(require_super_admin)]


def configured_client() -> SooperfareClient:
    return SooperfareClient()


class SooperfareOperationInfo(BaseModel):
    key: str
    title: str
    method: str
    supplier_path: str
    gateway_path: str
    writes: bool = Field(description="Changes a booking at SooperFare; needs SOOPERFARE_RAW_WRITE_ENABLED.")


@router.get("/raw/operations", response_model=list[SooperfareOperationInfo])
async def list_operations(viewer: SuperAdmin) -> list[SooperfareOperationInfo]:
    """All SooperFare calls the raw console can send."""
    return [SooperfareOperationInfo(key=o.key, title=o.title, method=o.method, supplier_path=o.path,
                          gateway_path=f"{settings.api_prefix}{PREFIX}/raw{o.path}", writes=o.writes) for o in OPERATIONS]


def _error(operation: Operation, error: SooperfareError) -> JSONResponse:
    if isinstance(error, SooperfareNotConfigured):
        return JSONResponse({"detail": error.public_message, "code": error.error_code}, status_code=503)
    if operation.writes and error.error_code in ("timeout", "unreachable"):
        return JSONResponse({"detail": "No answer from SooperFare: the outcome is unknown. Check the booking list "
                                       "before trying again.", "code": "outcome_unknown"}, status_code=504)
    status = 504 if error.error_code == "timeout" else 502
    return JSONResponse({"detail": error.public_message, "code": error.error_code}, status_code=status)


def _register(operation: Operation) -> None:
    async def endpoint(
        request: Request,
        viewer: SuperAdmin,
        payload: Annotated[Any, Body(description="SooperFare's documented request body, sent unchanged.")] = None,
        x_sf_client_currency: Annotated[str | None, Header(max_length=8)] = None,
        x_sf_client_locale: Annotated[str | None, Header(max_length=8)] = None,
    ):
        if operation.writes and not settings.sooperfare_raw_write_enabled:
            return JSONResponse({"detail": "Calls that change bookings at SooperFare are turned off "
                                           "(SOOPERFARE_RAW_WRITE_ENABLED).", "code": "writes_disabled"}, status_code=409)
        headers = {name: value for name, value in (("X-SF-Client-Currency", x_sf_client_currency),
                                                   ("X-SF-Client-Locale", x_sf_client_locale)) if value}
        try:
            status, body = await configured_client().raw(
                operation.method, operation.path, payload, query=dict(request.query_params), headers=headers,
                retry_login=not operation.writes)
        except SooperfareError as error:
            return _error(operation, error)
        # Who called what, never the body (it can hold passenger details).
        logger.info("SooperFare raw call", extra={"operation": operation.key, "user_id": viewer.user_id,
                                                  "supplier_status": status})
        return JSONResponse(body, status_code=502 if status in (401, 403) else status,
                            headers={"X-Supplier-Status": str(status)})

    writes = " Changes a booking at SooperFare: needs SOOPERFARE_RAW_WRITE_ENABLED, never retried." if operation.writes else ""
    router.add_api_route(
        f"/raw{operation.path}", endpoint, methods=[operation.method],
        name=f"sooperfare_raw_{operation.key}", operation_id=f"sooperfare_raw_{operation.key}",
        responses={200: {"description": "SooperFare's answer, unchanged."},
                   409: {"description": "Calls that change bookings are turned off."},
                   502: {"description": "SooperFare error or unreachable."},
                   503: {"description": "SooperFare isn't configured."}, 504: {"description": "Timeout."}},
        summary=f"SooperFare raw: {operation.title}",
        description=f"SooperFare {operation.method} {operation.path}, sent and answered unchanged.{writes}",
    )


for _operation in OPERATIONS:
    _register(_operation)
