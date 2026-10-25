"""Scoped agency-machine entry points reuse existing group aggregation."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.routes.group_ticketing import (
    GroupInventoryResponse,
    GroupSearchRequest,
    fetch_group_inventory,
    search_group_inventory,
)
from app.core.authentication import AuthenticatedMachine
from app.core.config import settings
from app.core.integration_security import authorize_machine, require_machine


async def require_search(
    machine: Annotated[AuthenticatedMachine, Depends(require_machine)],
):
    return await authorize_machine(machine, settings.integration_search_scope)


router = APIRouter(
    prefix="/integrations/group-ticketing",
    tags=["agency integrations"],
    dependencies=[Depends(require_search)],
)


@router.get("/inventory", response_model=GroupInventoryResponse)
async def inventory():
    return await fetch_group_inventory()


@router.post("/search", response_model=GroupInventoryResponse)
async def search(body: GroupSearchRequest):
    return await search_group_inventory(body)
