"""Staff notifications (holds, booking requests, restricted-airline requests). Back-office roles only."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.core.field_visibility import BackOfficeUser, require_back_office
from app.services import notifications

router = APIRouter(prefix="/admin/notifications", tags=["admin notifications"])


class NotificationView(BaseModel):
    id: str
    kind: str
    reference: str | None = None
    title: str
    details: dict[str, Any]
    emailed: bool
    created_at: datetime


class NotificationList(BaseModel):
    items: list[NotificationView]
    email_enabled: bool = Field(description="Alerts are also emailed to staff. False until email is set up on the server "
                                            "(deploy/aws-setup.sh: SES sender and recipients); alerts still appear here.")


@router.get("", response_model=NotificationList)
async def list_notifications(viewer: Annotated[BackOfficeUser, Depends(require_back_office)],
                             limit: Annotated[int, Query(ge=1, le=200)] = 50) -> NotificationList:
    """Newest first."""
    rows = await run_in_threadpool(notifications.recent, limit)
    return NotificationList(items=[NotificationView(**row) for row in rows], email_enabled=notifications.email_configured())
