"""Scoped agency-machine entry points reuse existing group aggregation."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.routes.group_ticketing import GroupSearchRequest, inventory_for, search_for
from app.core.field_visibility import NONE
from app.services.group_ticketing_view import GroupInventoryView
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


# Agency machines: the agency view (no supplier, cost or raw supplier fields; D23).
@router.get("/inventory", response_model=GroupInventoryView, response_model_exclude_none=True)
async def inventory():
    return await inventory_for(NONE)


@router.post("/search", response_model=GroupInventoryView, response_model_exclude_none=True)
async def search(body: GroupSearchRequest):
    return await search_for(body, NONE)
