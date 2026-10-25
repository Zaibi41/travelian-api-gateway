from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from app.core.agency_security import require_local_admin
from app.core.authentication import AuthenticatedUser
from app.db import admin_users as store
from app.schemas.admin_users import (
    AdminOverview,
    RoleRecord,
    UserAdminEvent,
    UserRecord,
    UserRoleChange,
    UserStatusChange,
)

router = APIRouter(
    prefix="/admin",
    tags=["user administration"],
    dependencies=[Depends(require_local_admin)],
)
Admin = Annotated[AuthenticatedUser, Depends(require_local_admin)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100000)]


@router.get("/users", response_model=list[UserRecord])
async def list_users(
    limit: Limit = 20,
    offset: Offset = 0,
    role_id: Annotated[int | None, Query(ge=1, le=3)] = None,
    active: bool | None = None,
):
    return await run_in_threadpool(store.list_users, limit, offset, role_id, active)


@router.get("/users/{user_id}", response_model=UserRecord)
async def get_user(user_id: UUID):
    return await run_in_threadpool(store.get_user, user_id)


@router.patch("/users/{user_id}/role", response_model=UserRecord)
async def change_role(user_id: UUID, body: UserRoleChange, user: Admin):
    return await run_in_threadpool(
        store.change_user, user, user_id, body, "role_changed"
    )


@router.patch("/users/{user_id}/status", response_model=UserRecord)
async def change_status(user_id: UUID, body: UserStatusChange, user: Admin):
    return await run_in_threadpool(
        store.change_user, user, user_id, body, "status_changed"
    )


@router.get("/users/{user_id}/history", response_model=list[UserAdminEvent])
async def user_history(user_id: UUID, limit: Limit = 20, offset: Offset = 0):
    return await run_in_threadpool(store.history, user_id, limit, offset)


@router.get("/roles", response_model=list[RoleRecord])
async def list_roles():
    return await run_in_threadpool(store.roles)


@router.get("/dashboard/overview", response_model=AdminOverview)
async def dashboard_overview():
    return await run_in_threadpool(store.overview)
