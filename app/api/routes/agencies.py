from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query
from starlette.concurrency import run_in_threadpool

from app.core.agency_security import (
    require_agency_read,
    require_agency_review,
    require_agency_write,
)
from app.core.authentication import AuthenticatedUser, require_authenticated_user
from app.db import agencies as store
from app.db.agency_applications import create_application
from app.schemas.agencies import AgencyAction, AgencyEvent, AgencyUpdate
from app.schemas.agency_applications import (
    AgencyApplicationRequest,
    AgencyApplicationResponse,
)

router = APIRouter(tags=["agencies"])
User = Annotated[AuthenticatedUser, Depends(require_authenticated_user)]
Reader = Annotated[AuthenticatedUser, Depends(require_agency_read)]
Writer = Annotated[AuthenticatedUser, Depends(require_agency_write)]
Reviewer = Annotated[AuthenticatedUser, Depends(require_agency_review)]
AgencyId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100000)]


@router.post("/agencies", response_model=AgencyApplicationResponse, status_code=201)
async def create_agency(body: AgencyApplicationRequest, user: User):
    return await run_in_threadpool(create_application, user, body)


@router.get("/agencies/me", response_model=AgencyApplicationResponse)
async def own_agency(user: User):
    return await run_in_threadpool(store.detail, user)


@router.patch("/agencies/me", response_model=AgencyApplicationResponse)
async def update_own_agency(body: AgencyUpdate, user: User):
    return await run_in_threadpool(store.change, user, body, "profile_updated")


@router.post("/agencies/me/resubmit", response_model=AgencyApplicationResponse)
async def resubmit_agency(body: AgencyAction, user: User):
    return await run_in_threadpool(store.change, user, body, "resubmitted")


@router.get("/agencies/me/history", response_model=list[AgencyEvent])
async def own_history(user: User, limit: Limit = 20, offset: Offset = 0):
    return await run_in_threadpool(store.history, user, None, limit, offset)


@router.get("/admin/agencies", response_model=list[AgencyApplicationResponse])
async def list_agencies(
    user: Reader,
    limit: Limit = 20,
    offset: Offset = 0,
    application_status: Literal[
        "pending_review", "more_information_required", "approved", "rejected"
    ]
    | None = None,
    agency_status: Literal["active", "suspended"] | None = None,
):
    return await run_in_threadpool(
        store.listing, limit, offset, application_status, agency_status
    )


@router.get("/admin/agencies/{agency_id}", response_model=AgencyApplicationResponse)
async def admin_agency(agency_id: AgencyId, user: Reader):
    return await run_in_threadpool(store.detail, user, agency_id)


@router.patch("/admin/agencies/{agency_id}", response_model=AgencyApplicationResponse)
async def update_agency(agency_id: AgencyId, body: AgencyUpdate, user: Writer):
    return await run_in_threadpool(
        store.change, user, body, "profile_updated", agency_id
    )


@router.get("/admin/agencies/{agency_id}/history", response_model=list[AgencyEvent])
async def admin_history(
    agency_id: AgencyId, user: Reader, limit: Limit = 20, offset: Offset = 0
):
    return await run_in_threadpool(store.history, user, agency_id, limit, offset)


def register_action(path: str, action: str):
    async def endpoint(agency_id: AgencyId, body: AgencyAction, user: Reviewer):
        return await run_in_threadpool(store.change, user, body, action, agency_id)

    router.add_api_route(
        "/admin/agencies/{agency_id}/" + path,
        endpoint,
        methods=["POST"],
        response_model=AgencyApplicationResponse,
        name="agency_" + path,
    )


for path, action in [
    ("approve", "approved"),
    ("reject", "rejected"),
    ("request-information", "information_requested"),
    ("suspend", "suspended"),
    ("reactivate", "reactivated"),
]:
    register_action(path, action)
