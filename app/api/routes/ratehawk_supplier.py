"""RateHawk supplier console: every documented RateHawk operation (48), Super Admin only.

POST /v1/admin/suppliers/ratehawk/operations/{key} with {"payload": {...}} sends the payload as that
operation's documented body (or `?data=` for GET operations) and returns RateHawk's `data` unchanged.
Raw supplier data (net prices, commission, hashes) is why this is Super Admin only (D23).

Operations that change supplier state (booking, cancel, order groups, profiles) answer 409 unless
RATEHAWK_WRITE_ENABLED is on, and are never retried: a timeout there is reported as "outcome unknown".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.core.field_visibility import BackOfficeUser, require_super_admin
from app.vendors.hotels.ratehawk.client import RateHawkError, RateHawkNotConfigured, RateHawkWritesDisabled, configured_client
from app.vendors.hotels.ratehawk.operations import OPERATIONS, Operation

router = APIRouter(
    prefix="/admin/suppliers/ratehawk",
    tags=["ratehawk supplier"],
    responses={401: {"description": "Not signed in."}, 403: {"description": "Super Admin only."}},
)
SuperAdmin = Annotated[BackOfficeUser, Depends(require_super_admin)]


class OperationInfo(BaseModel):
    key: str
    title: str
    group: str
    supplier_method: str
    supplier_path: str
    writes: bool = Field(description="Changes supplier state; needs RATEHAWK_WRITE_ENABLED.")
    download: bool = Field(description="Answers with a file.")


class OperationCall(BaseModel):
    payload: dict[str, Any] | None = Field(
        None, description="The operation's documented request body (for GET operations, the `data` query object). "
                          "Omit for operations without input.")


class OperationResult(BaseModel):
    operation: str
    data: Any


@router.get("/operations", response_model=list[OperationInfo])
async def list_operations(viewer: SuperAdmin) -> list[OperationInfo]:
    """All RateHawk operations the gateway can call."""
    return [OperationInfo(key=o.key, title=o.title, group=o.group, supplier_method=o.method, supplier_path=o.path,
                          writes=o.writes, download=o.download) for o in OPERATIONS]


def _error(operation: Operation, error: RateHawkError) -> JSONResponse:
    if isinstance(error, RateHawkNotConfigured):
        return JSONResponse({"detail": error.public_message, "code": error.error_code}, status_code=503)
    if isinstance(error, RateHawkWritesDisabled):
        return JSONResponse({"detail": error.public_message, "code": error.error_code}, status_code=409)
    if operation.writes and error.error_code in ("timeout", "upstream_error"):
        return JSONResponse({"detail": "No answer from RateHawk: the outcome is unknown. Check the booking status "
                                       "before trying again.", "code": "outcome_unknown"}, status_code=504)
    status = 429 if error.status == 429 else 504 if error.error_code == "timeout" else 502
    body: dict[str, Any] = {"detail": error.public_message, "code": error.error_code}
    if error.debug is not None:
        body["supplier_debug"] = error.debug
    return JSONResponse(body, status_code=status)


def _register(operation: Operation) -> None:
    responses = {409: {"description": "Write operations are turned off."}, 502: {"description": "RateHawk error."},
                 503: {"description": "RateHawk isn't configured."}, 504: {"description": "Timeout."}}

    if operation.download:
        async def endpoint(call: OperationCall, viewer: SuperAdmin):
            try:
                file = await configured_client().download(operation.key, call.payload or {})
            except RateHawkError as error:
                return _error(operation, error)
            headers = {"Content-Disposition": f'attachment; filename="{file.filename or operation.key + ".pdf"}"'}
            return Response(file.content, media_type=file.content_type, headers=headers)

        responses[200] = {"description": "The file.", "content": {"application/pdf": {}}}
        response_model = None
    else:
        async def endpoint(call: OperationCall, viewer: SuperAdmin):
            try:
                data = await configured_client().call(operation.key, call.payload)
            except RateHawkError as error:
                return _error(operation, error)
            return OperationResult(operation=operation.key, data=data)

        response_model = OperationResult

    writes = " Changes supplier state: needs RATEHAWK_WRITE_ENABLED, never retried." if operation.writes else ""
    router.add_api_route(
        f"/operations/{operation.key}", endpoint, methods=["POST"], response_model=response_model,
        name=f"ratehawk_{operation.key}", operation_id=f"ratehawk_{operation.key}", responses=responses,
        summary=operation.title, description=f"RateHawk {operation.method} {operation.path}.{writes}",
    )


for _operation in OPERATIONS:
    _register(_operation)
