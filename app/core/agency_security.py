"""Local roles authorize administration; Cognito only verifies identity."""

from typing import Annotated

from fastapi import Depends
from starlette.concurrency import run_in_threadpool

from app.core.authentication import AuthenticatedUser, require_authenticated_user
from app.db.users import require_admin


async def require_local_admin(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
):
    return await run_in_threadpool(require_admin, user)


# All three operations require the explicitly provisioned local super_admin
# role in the current one-role model. No fallback to token group membership.
require_agency_read = require_local_admin
require_agency_write = require_local_admin
require_agency_review = require_local_admin
