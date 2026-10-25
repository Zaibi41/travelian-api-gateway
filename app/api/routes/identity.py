from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.authentication import AuthenticatedUser, require_authenticated_user
from app.db.users import local_identity

router = APIRouter(tags=["identity"])


class CurrentUser(BaseModel):
    subject: str
    issuer: str
    client_id: str
    user_id: str
    role_id: int
    role: Literal["customer", "agency_owner", "super_admin"]
    agency_id: str | None = None
    agency_status: Literal["active", "suspended"] | None = None
    application_status: str | None = None


@router.get("/auth/me", response_model=CurrentUser)
async def current_identity(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(local_identity, user)
