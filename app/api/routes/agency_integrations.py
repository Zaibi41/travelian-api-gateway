from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.core.agency_security import require_agency_read, require_agency_write
from app.core.authentication import (
    ISSUER_PATTERN,
    AuthenticatedMachine,
    AuthenticatedUser,
)
from app.core.config import settings
from app.core.integration_security import authorize_machine, require_machine
from app.db import agency_integrations as store

router = APIRouter(tags=["agency integrations"])


class IntegrationRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(pattern=r"^[a-z0-9]{1,128}$")
    agency_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    scopes: list[str] = Field(min_length=1, max_length=10)


class IntegrationRecord(BaseModel):
    client_id: str
    agency_id: str
    scopes: list[str]
    enabled: bool


def issuer():
    value = settings.cognito_issuer
    if not value or not ISSUER_PATTERN.fullmatch(value):
        raise HTTPException(503, "Integration authentication is not configured.")
    return value


@router.post(
    "/admin/agency-integrations", response_model=IntegrationRecord, status_code=201
)
async def register(
    body: IntegrationRegistration,
    user: Annotated[AuthenticatedUser, Depends(require_agency_write)],
):
    allowed = {
        scope
        for scope in [
            settings.integration_search_scope,
            settings.integration_booking_create_scope,
            settings.integration_booking_details_scope,
        ]
        if scope
    }
    if not allowed:
        raise HTTPException(503, "Integration permissions are not configured.")
    if body.client_id == settings.cognito_app_client_id or not set(
        body.scopes
    ).issubset(allowed):
        raise HTTPException(422, "Invalid integration client or permissions.")
    return await run_in_threadpool(
        store.register,
        issuer(),
        body.client_id,
        body.agency_id,
        sorted(set(body.scopes)),
    )


@router.get("/admin/agency-integrations", response_model=list[IntegrationRecord])
async def listing(
    agency_id: Annotated[str, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")],
    user: Annotated[AuthenticatedUser, Depends(require_agency_read)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    return await run_in_threadpool(
        store.list_integrations, issuer(), agency_id, limit, offset
    )


@router.post("/admin/agency-integrations/{client_id}/revoke", status_code=204)
async def revoke(
    client_id: str, user: Annotated[AuthenticatedUser, Depends(require_agency_write)]
):
    await run_in_threadpool(store.revoke, issuer(), client_id)
    return Response(status_code=204)


@router.get("/auth/integration")
async def integration_identity(
    machine: Annotated[AuthenticatedMachine, Depends(require_machine)],
    response: Response,
):
    agency_id = await authorize_machine(machine, settings.integration_search_scope)
    response.headers["Cache-Control"] = "no-store"
    return {
        "identity_type": "agency_integration",
        "client_id": machine.client_id,
        "agency_id": agency_id,
    }
