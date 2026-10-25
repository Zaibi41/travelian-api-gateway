from typing import Annotated

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.core.authentication import AuthenticatedUser, require_authenticated_user
from app.db.agency_applications import create_application, get_application
from app.schemas.agency_applications import (
    AgencyApplicationRequest,
    AgencyApplicationResponse,
)

router = APIRouter(prefix="/agencies/applications", tags=["agency applications"])
User = Annotated[AuthenticatedUser, Depends(require_authenticated_user)]


@router.post("", response_model=AgencyApplicationResponse, status_code=201)
async def submit_agency_application(body: AgencyApplicationRequest, user: User):
    return await run_in_threadpool(create_application, user, body)


@router.get("/me", response_model=AgencyApplicationResponse)
async def applicant_status(user: User):
    return await run_in_threadpool(get_application, user)
